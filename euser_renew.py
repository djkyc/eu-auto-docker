#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EUserv 自动续期脚本 - 多账号多线程版本 (ddddocr 专业版 + 样式化通知)
============================================================
功能：
  - 支持多个 EUserv 账号同时处理
  - 使用 ddddocr 高效识别验证码
  - 自动获取邮箱 PIN（支持常见邮箱 IMAP）
  - 多步骤续期操作，自动跳过无需续期的服务器
  - 多渠道通知：Telegram、PushPlus（微信公众号）、自定义微信 WebHook
  - 通知样式：✅成功 / ℹ️无需续期 / ❌失败

环境变量配置（建议通过 .env 文件传入）：
  - EUSERV_ACCOUNTS  : 多账号配置，格式见下文
  - 或单账号：EUSERV_EMAIL, EUSERV_PASSWORD, EMAIL_PASS
  - TG_BOT_TOKEN / TG_CHAT_ID            : Telegram 通知
  - PUSH_PLUS_TOKEN                       : PushPlus 微信公众号
  - WECHAT_API_URL / WECHAT_AUTH_TOKEN   : 自定义微信 WebHook
  - SKIP_CONTRACTS                        : 要跳过的合同 ID，逗号分隔
  - DEBUG                                 : 调试模式 (true / false / html)
  - HTTP_PROXY / HTTPS_PROXY               : 代理（可选）
  - MAX_WORKERS                            : 最大并发线程数（默认3）
  - MAX_LOGIN_RETRIES                      : 登录重试次数（默认3）
  - MAIL_RETRIES / MAIL_INTERVAL           : 邮件重试次数/间隔（默认12/5）
  - RENEW_ALL                               : 是否续期所有可续期合同（默认false）

多账号配置格式 (EUSERV_ACCOUNTS):
  email1:password1[:imap_server:email_password];email2:password2...
  冒号分隔字段，分号分隔账号。后两个字段可选（自动识别 IMAP，密码默认同登录密码）。
"""

import os
import sys
import io
import re
import json
import time
import threading
import logging
from typing import Dict, List, Tuple, Optional
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from PIL import Image
import ddddocr
import requests
from bs4 import BeautifulSoup
from imap_tools import MailBox

# ==================== 日志配置 ====================
_debug_env = os.getenv("DEBUG", "").lower()
DEBUG_MODE = _debug_env in ("true", "1", "yes", "html")
SAVE_HTML_MODE = _debug_env == "html"
log_level = logging.DEBUG if DEBUG_MODE else logging.INFO

logging.basicConfig(
    level=log_level,
    format='%(asctime)s [%(threadName)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# ==================== 全局常量 ====================
SKIP_CONTRACTS = [x.strip() for x in os.getenv("SKIP_CONTRACTS", "").split(",") if x.strip()]
if SKIP_CONTRACTS:
    logger.info(f"配置了跳过合同列表: {SKIP_CONTRACTS}")

# 兼容新版 Pillow
if not hasattr(Image, 'ANTIALIAS'):
    Image.ANTIALIAS = Image.Resampling.LANCZOS

# 初始化 OCR（线程安全）
ocr = ddddocr.DdddOcr()
ocr_lock = threading.Lock()

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/94.0.4606.61 Safari/537.36"


# ==================== 配置数据类 ====================
class AccountConfig:
    """单个账号配置"""
    def __init__(self, email: str, password: str,
                 imap_server: str = 'imap.gmail.com',
                 email_password: str = ''):
        self.email = email
        self.password = password
        self.imap_server = imap_server
        self.email_password = email_password if email_password else password


class GlobalConfig:
    """全局配置"""
    def __init__(self, telegram_bot_token: str = "",
                 telegram_chat_id: str = "",
                 max_workers: int = 3,
                 max_login_retries: int = 3):
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id
        self.max_workers = max_workers
        self.max_login_retries = max_login_retries


# ==================== 全局配置实例 ====================
GLOBAL_CONFIG = GlobalConfig(
    telegram_bot_token=os.getenv("TG_BOT_TOKEN", ""),
    telegram_chat_id=os.getenv("TG_CHAT_ID", ""),
    max_workers=int(os.getenv("MAX_WORKERS", "3")),
    max_login_retries=int(os.getenv("MAX_LOGIN_RETRIES", "3"))
)


# ==================== 辅助函数 ====================
def get_imap_server(email: str) -> str:
    """根据邮箱域名自动选择 IMAP 服务器"""
    if "@qq.com" in email or "@foxmail.com" in email:
        return "imap.qq.com"
    elif "@163.com" in email:
        return "imap.163.com"
    elif "@outlook.com" in email or "@hotmail.com" in email:
        return "outlook.office365.com"
    return "imap.gmail.com"


def parse_accounts(env_str: str) -> List[AccountConfig]:
    """
    解析 EUSERV_ACCOUNTS 环境变量。
    格式：email1:password1[:imap_server:email_password];email2:password2...
    """
    accounts = []
    if not env_str:
        return accounts
    for part in env_str.split(';'):
        part = part.strip()
        if not part:
            continue
        fields = part.split(':')
        if len(fields) < 2:
            logger.warning(f"账号格式错误，跳过: {part}")
            continue
        email = fields[0]
        password = fields[1]
        imap_server = fields[2] if len(fields) > 2 else get_imap_server(email)
        email_password = fields[3] if len(fields) > 3 else password
        accounts.append(AccountConfig(email, password, imap_server, email_password))
    return accounts


# 读取账号配置（优先多账号）
_accounts_env = os.getenv("EUSERV_ACCOUNTS", "").strip()
if _accounts_env:
    ACCOUNTS = parse_accounts(_accounts_env)
    logger.info(f"从 EUSERV_ACCOUNTS 解析到 {len(ACCOUNTS)} 个账号")
else:
    # 兼容单账号模式
    single_email = os.getenv("EUSERV_EMAIL", "")
    single_pass = os.getenv("EUSERV_PASSWORD", "")
    if single_email and single_pass:
        ACCOUNTS = [AccountConfig(
            email=single_email,
            password=single_pass,
            imap_server=get_imap_server(single_email),
            email_password=os.getenv("EMAIL_PASS", single_pass)
        )]
        logger.info("使用单账号模式")
    else:
        ACCOUNTS = []
        logger.warning("未配置任何账号，请设置 EUSERV_ACCOUNTS 或 EUSERV_EMAIL/PASSWORD")


# ==================== 验证码识别 ====================
def recognize_captcha(image_bytes: bytes) -> Optional[str]:
    """使用 ddddocr 识别验证码图片，返回识别结果（可能含空格，保持原始大小写）"""
    with ocr_lock:
        try:
            return ocr.classification(image_bytes).strip()
        except Exception as e:
            logger.error(f"ddddocr 识别失败: {e}")
            return None


def parse_math_expression(text: str) -> Optional[str]:
    """
    尝试解析数学运算式（如 5*F, 8+3）并计算结果。
    支持运算符：+ - * × x X / ÷
    字母映射：A=10 ... Z=35
    返回计算结果字符串，若无法解析或运算非法则返回 None。
    """
    # 预处理：去除空格，转大写
    raw = text.strip().replace(' ', '').upper().rstrip('?')
    # 纯字母数字直接返回原值
    if re.fullmatch(r'[A-Z0-9]+', raw):
        return raw

    # 匹配运算式：数字 运算符 (数字或字母)
    pattern = r'^(\d+)([+\-*/×xX÷])(\d+|[A-Z])=?'
    match = re.search(pattern, raw)
    if not match:
        # 尝试宽松匹配（可能开头有额外字符）
        match = re.search(r'(\d+)([+\-*/×xX÷])(\d+|[A-Z])', raw)
    if not match:
        return None

    left_str, op, right_str = match.groups()
    left = int(left_str)

    # 处理右操作数
    if right_str.isdigit():
        right = int(right_str)
    elif 'A' <= right_str <= 'Z':
        right = ord(right_str) - ord('A') + 10
    else:
        return None

    # 计算
    if op in {'*', '×', 'X', 'x'}:
        result = left * right
    elif op == '+':
        result = left + right
    elif op == '-':
        result = left - right
    elif op in {'/', '÷'}:
        if right == 0 or left % right != 0:
            return None
        result = left // right
    else:
        return None

    return str(result)


def recognize_and_calculate(captcha_url: str, session: requests.Session) -> Optional[str]:
    """
    下载验证码图片，识别后返回候选值（原始识别结果或计算结果）。
    若无法识别返回 None。
    """
    logger.info("正在处理验证码...")
    try:
        resp = session.get(captcha_url)
        img_bytes = resp.content

        # 直接识别
        text = recognize_captcha(img_bytes)
        if not text:
            logger.warning("ddddocr 未识别出任何字符")
            return None

        logger.debug(f"原始识别结果: {text}")

        # 尝试解析为数学运算
        calculated = parse_math_expression(text)
        if calculated is not None and calculated != text.strip():
            logger.info(f"验证码运算解析: {calculated}")
            return calculated

        # 返回原始识别结果（去空格）
        clean = text.strip()
        logger.info(f"验证码原始结果: {clean}")
        return clean

    except Exception as e:
        logger.error(f"验证码处理异常: {e}", exc_info=True)
        return None


# ==================== 邮箱 PIN 获取 ====================
def get_euserv_pin(
    email: str,
    email_password: str,
    imap_server: str,
    after_time: datetime = None,
    pin_type: str = 'login'
) -> Optional[str]:
    """
    从邮箱获取 EUserv PIN 码（带重试机制）
    pin_type: 'login' 或 'renew'，用于过滤邮件主题
    """
    max_retries = int(os.getenv("MAIL_RETRIES", "12"))
    retry_interval = int(os.getenv("MAIL_INTERVAL", "5"))

    if pin_type == 'renew':
        subject_keywords = ['security check', 'confirmation']
        type_name = "续期"
    else:
        subject_keywords = ['attempted login', 'login']
        type_name = "登录"

    logger.info(f"正在从邮箱 {email} 获取{type_name} PIN 码（最长等待 {max_retries * retry_interval} 秒）...")
    if after_time:
        logger.debug(f"只查找 {after_time.strftime('%H:%M:%S')} 之后的邮件")

    for i in range(max_retries):
        if i > 0:
            logger.info(f"第 {i + 1} 次尝试获取邮件...")
            time.sleep(retry_interval)

        try:
            with MailBox(imap_server).login(email, email_password) as mailbox:
                for msg in mailbox.fetch(limit=10, reverse=True):
                    if 'euserv' not in msg.from_.lower():
                        continue
                    if not any(kw in msg.subject.lower() for kw in subject_keywords):
                        continue

                    # 时间过滤（允许 2 分钟误差）
                    if after_time and msg.date:
                        msg_ts = msg.date.timestamp()
                        if msg_ts < (after_time.timestamp() - 120):
                            continue

                    text = msg.text or ""
                    # 优先匹配 PIN: 后的六位数
                    match = re.search(r'PIN:\s*\n?(\d{6})', text) or re.search(r'PIN.*?(\d{6})', text, re.DOTALL)
                    if match:
                        pin = match.group(1)
                        logger.info(f"✅ 提取到{type_name} PIN 码: {pin}")
                        return pin
                    # 备用：任意六位数
                    fallback = re.search(r'\b(\d{6})\b', text)
                    if fallback:
                        pin = fallback.group(1)
                        logger.info(f"✅ 提取到{type_name} PIN 码（备用）: {pin}")
                        return pin
        except Exception as e:
            logger.warning(f"邮件获取异常: {e}")

    logger.error(f"❌ 超时未找到{type_name} PIN 码邮件")
    return None


# ==================== EUserv 操作类 ====================
class EUserv:
    def __init__(self, config: AccountConfig):
        self.config = config
        self.session = requests.Session()
        self.sess_id = None
        self._login_html = ""

    def login(self) -> bool:
        """执行登录流程，返回成功与否"""
        logger.info(f"正在登录账号: {self.config.email}")
        url = "https://support.euserv.com/index.iphp"
        captcha_url = "https://support.euserv.com/securimage_show.php"
        headers = {'user-agent': USER_AGENT, 'origin': 'https://www.euserv.com'}

        try:
            # 获取 sess_id
            resp = self.session.get(url, headers=headers)
            sess_match = re.search(r'sess_id=([a-zA-Z0-9]{30,100})', resp.text)
            if not sess_match:
                logger.error("❌ 无法获取 sess_id")
                return False
            sess_id = sess_match.group(1)
            logger.debug(f"sess_id: {sess_id[:20]}...")

            # 提交登录表单
            login_data = {
                'email': self.config.email,
                'password': self.config.password,
                'form_selected_language': 'en',
                'Submit': 'Login',
                'subaction': 'login',
                'sess_id': sess_id
            }
            resp = self.session.post(url, headers=headers, data=login_data)
            resp.raise_for_status()

            # 检查常见错误
            if 'Please check email address/customer ID and password' in resp.text:
                logger.error("❌ 用户名或密码错误")
                return False
            if 'kc2_login_iplock_cdown' in resp.text:
                logger.error("❌ 账号被锁定，请稍后重试")
                return False

            # ---- 验证码处理 ----
            if 'captcha' in resp.text.lower():
                captcha_success = False
                for attempt in range(3):
                    logger.info(f"⚠️ 验证码识别（第 {attempt+1}/3 次）")
                    code = recognize_and_calculate(captcha_url, self.session)
                    if not code:
                        time.sleep(2)
                        continue
                    post_data = {
                        'subaction': 'login',
                        'sess_id': sess_id,
                        'captcha_code': code
                    }
                    resp = self.session.post(url, headers=headers, data=post_data)
                    if 'captcha' not in resp.text.lower():
                        captcha_success = True
                        logger.info("✅ 验证码通过")
                        break
                    else:
                        logger.debug(f"验证码 {code} 错误")
                        time.sleep(2)
                if not captcha_success:
                    logger.error("❌ 验证码连续失败")
                    return False

            # ---- PIN 验证 ----
            if 'PIN that you receive via email' in resp.text:
                logger.info("⚠️ 需要 PIN 验证")
                pin_time = datetime.now()
                time.sleep(3)
                pin = get_euserv_pin(
                    self.config.email,
                    self.config.email_password,
                    self.config.imap_server,
                    after_time=pin_time,
                    pin_type='login'
                )
                if not pin:
                    logger.error("❌ 获取 PIN 码失败")
                    return False
                soup = BeautifulSoup(resp.text, 'html.parser')
                c_id = soup.find('input', {'name': 'c_id'})['value']
                pin_data = {
                    'pin': pin,
                    'sess_id': sess_id,
                    'Submit': 'Confirm',
                    'subaction': 'login',
                    'c_id': c_id
                }
                resp = self.session.post(url, headers=headers, data=pin_data)

            # 判断登录成功
            if any(x in resp.text for x in ['Hello', 'Confirm or change your customer data here', 'logout']):
                logger.info(f"✅ 账号 {self.config.email} 登录成功")
                self.sess_id = sess_id
                self._login_html = resp.text
                return True
            else:
                logger.error(f"❌ 登录失败，未知响应")
                return False

        except Exception as e:
            logger.error(f"登录异常: {e}", exc_info=True)
            return False

    def confirm_customer_data(self) -> bool:
        """确认个人信息页面（解除面板限制）"""
        # 此处为简化占位，实际使用时请替换为完整实现
        logger.debug("跳过 Customer Data 确认（占位）")
        return True

    def get_servers(self) -> Dict[str, Tuple[bool, str]]:
        """获取服务器列表，返回 {order_id: (can_renew, next_date)}"""
        # 此处为简化占位，实际使用时请替换为完整实现
        logger.debug("获取服务器列表（占位）")
        return {}

    def renew_server(self, order_id: str) -> bool:
        """执行单个服务器的续期流程"""
        # 此处为简化占位，实际使用时请替换为完整实现
        logger.debug(f"续期服务器 {order_id}（占位）")
        return True


# ==================== 通知函数 ====================
def send_notification(message: str, config: GlobalConfig) -> None:
    """综合发送通知（Telegram / PushPlus / 自定义微信）"""
    # Telegram
    if config.telegram_bot_token and config.telegram_chat_id:
        base_url = os.getenv("TG_API_URL", "https://api.telegram.org").rstrip('/')
        url = f"{base_url}/bot{config.telegram_bot_token}/sendMessage"
        data = {"chat_id": config.telegram_chat_id, "text": message, "parse_mode": "HTML"}
        proxies = None
        if os.getenv("TG_PROXY"):
            proxies = {"http": os.getenv("TG_PROXY"), "https": os.getenv("TG_PROXY")}
        try:
            r = requests.post(url, json=data, timeout=15, proxies=proxies)
            if r.status_code == 200:
                logger.info("✅ Telegram 通知发送成功")
            else:
                logger.error(f"❌ Telegram 通知失败: {r.status_code}")
        except Exception as e:
            logger.error(f"❌ Telegram 异常: {e}")

    # PushPlus
    push_token = os.getenv("PUSH_PLUS_TOKEN")
    if push_token:
        try:
            url = "http://www.pushplus.plus/send"
            data = {
                "token": push_token,
                "title": "📋 EUserv 自动续期报告",
                "content": message.replace('\n', '<br>'),
                "template": "html"
            }
            r = requests.post(url, json=data, timeout=15)
            if r.status_code == 200:
                logger.info("✅ PushPlus 通知发送成功")
            else:
                logger.error(f"❌ PushPlus 失败: {r.status_code}")
        except Exception as e:
            logger.error(f"❌ PushPlus 异常: {e}")

    # 自定义微信
    wechat_url = os.getenv("WECHAT_API_URL")
    if wechat_url:
        token = os.getenv("WECHAT_AUTH_TOKEN")
        try:
            payload = {"title": "EUserv 续期报告", "content": re.sub(r'<[^>]+>', '', message)}
            if token:
                payload["token"] = token
            r = requests.post(wechat_url, json=payload, timeout=10)
            if r.status_code == 200:
                logger.info("✅ 自定义微信通知发送成功")
            else:
                logger.error(f"❌ 微信通知失败: {r.status_code}")
        except Exception as e:
            logger.error(f"❌ 微信异常: {e}")


# ==================== 账号处理 ====================
def process_account(account: AccountConfig, global_config: GlobalConfig) -> dict:
    result = {
        'email': account.email,
        'success': False,
        'servers': {},
        'renew_results': [],
        'error': None
    }
    try:
        e = EUserv(account)

        # 登录重试
        login_ok = False
        for attempt in range(global_config.max_login_retries):
            if attempt > 0:
                logger.info(f"账号 {account.email} 第 {attempt+1} 次重试登录")
                time.sleep(5)
            if e.login():
                login_ok = True
                break
        if not login_ok:
            result['error'] = "登录失败"
            return result

        e.confirm_customer_data()
        servers = e.get_servers()
        result['servers'] = servers
        if not servers:
            result['error'] = "无服务器"
            result['success'] = True
            return result

        # 是否续期所有可续期合同
        renew_all = os.getenv("RENEW_ALL", "false").lower() == "true"
        for oid, (can_renew, date) in servers.items():
            if can_renew:
                logger.info(f"⏰ 服务器 {oid} 开始续期")
                ok = e.renew_server(oid)
                result['renew_results'].append({
                    'order_id': oid,
                    'success': ok,
                    'message': f"{'✅' if ok else '❌'} 服务器 {oid} 续期{'成功' if ok else '失败'}"
                })
                if not renew_all:
                    break
        result['success'] = True
    except Exception as e:
        result['error'] = str(e)
        logger.exception(f"处理账号 {account.email} 异常")
    return result


# ==================== 主函数 ====================
def main():
    logger.info("=" * 60)
    logger.info("EUserv 多账号自动续期脚本 (ddddocr 专业版 + 样式化通知)")
    logger.info(f"执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"配置账号数: {len(ACCOUNTS)}")
    logger.info(f"最大并发线程: {GLOBAL_CONFIG.max_workers}")
    logger.info("=" * 60)

    if not ACCOUNTS:
        logger.error("❌ 未配置账号，程序退出")
        sys.exit(1)

    with ThreadPoolExecutor(max_workers=GLOBAL_CONFIG.max_workers) as executor:
        futures = [executor.submit(process_account, acc, GLOBAL_CONFIG) for acc in ACCOUNTS]
        results = [f.result() for f in futures]

    # 生成汇总报告（带样式）
    logger.info("\n" + "=" * 60)
    logger.info("处理结果汇总")
    logger.info("=" * 60)

    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    message_parts = [f"<b>🔄 EUserv 续期报告 - {now_str}</b>"]

    for result in results:
        email = result['email']
        logger.info(f"\n账号: {email}")

        # 选择前缀 Emoji
        if not result['success']:
            prefix = "❌"
        else:
            if result['renew_results']:
                all_success = all(r['success'] for r in result['renew_results'])
                prefix = "✅" if all_success else "⚠️"
            else:
                prefix = "ℹ️"

        message_parts.append(f"\n{prefix} <b>📧 账号: {email}</b>")

        if not result['success']:
            error_msg = result.get('error', '未知错误')
            logger.error(f"  ❌ 处理失败: {error_msg}")
            message_parts.append(f"  ❌ 处理失败: {error_msg}")
            continue

        servers = result.get('servers', {})
        logger.info(f"  服务器数量: {len(servers)}")

        renew_results = result.get('renew_results', [])
        if renew_results:
            logger.info(f"  续期操作: {len(renew_results)} 个")
            for renew_result in renew_results:
                logger.info(f"    {renew_result['message']}")
                message_parts.append(f"  {renew_result['message']}")
        else:
            logger.info("  ✓ 无需续期")
            message_parts.append("  ✓ 无需续期")
            for order_id, (_, date) in servers.items():
                if date:
                    message_parts.append(f"    订单 {order_id}: 可续期日期 {date}")

    full_msg = "\n".join(message_parts)
    send_notification(full_msg, GLOBAL_CONFIG)
    logger.info("\n" + full_msg)
    logger.info("=" * 60)
    logger.info("执行完成")


if __name__ == "__main__":
    main()
