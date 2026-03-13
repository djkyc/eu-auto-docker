#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EUserv 自动续期脚本 - 多账号多线程版本
支持多账号配置、多线程并发处理、自动登录、验证码识别、检查到期状态、自动续期
并发送 Telegram / 微信通知（三种样式：登录失败、无需续期、续期成功/失败 + 下次续期日期）
"""

import os
import sys
import re
import json
import time
import threading
import logging
from typing import Dict, List, Tuple, Optional
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from PIL import Image
import ddddocr
import requests
from bs4 import BeautifulSoup
from imap_tools import MailBox

# ---------- 配置区 ----------
DEBUG_ENV = os.getenv("DEBUG", "").lower()
DEBUG_MODE = DEBUG_ENV in ("true", "1", "yes", "html")
SAVE_HTML_MODE = DEBUG_ENV == "html"
log_level = logging.DEBUG if DEBUG_MODE else logging.INFO

logging.basicConfig(
    level=log_level,
    format='%(asctime)s [%(threadName)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# 跳过合同列表（环境变量 SKIP_CONTRACTS，逗号分隔）
SKIP_CONTRACTS = [x.strip() for x in os.getenv("SKIP_CONTRACTS", "").split(",") if x.strip()]
if SKIP_CONTRACTS:
    logger.info(f"配置了跳过合同列表: {SKIP_CONTRACTS}")

# 兼容新版 Pillow
if not hasattr(Image, 'ANTIALIAS'):
    Image.ANTIALIAS = Image.Resampling.LANCZOS

# 全局 OCR 实例（线程安全）
ocr = ddddocr.DdddOcr()
ocr_lock = threading.Lock()

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/94.0.4606.61 Safari/537.36"

# ============== 配置数据类 ==============
class AccountConfig:
    """单个账号配置"""
    def __init__(self, email, password, imap_server='imap.gmail.com', email_password=''):
        self.email = email
        self.password = password
        self.imap_server = imap_server
        self.email_password = email_password if email_password else password


class GlobalConfig:
    """全局配置"""
    def __init__(self, telegram_bot_token="", telegram_chat_id="", max_workers=3, max_login_retries=3):
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id
        self.max_workers = max_workers
        self.max_login_retries = max_login_retries


# 全局配置实例
GLOBAL_CONFIG = GlobalConfig(
    telegram_bot_token=os.getenv("TG_BOT_TOKEN"),
    telegram_chat_id=os.getenv("TG_CHAT_ID"),
    max_workers=3,
    max_login_retries=3
)


def get_imap_server(email: str) -> str:
    """根据邮箱域名自动选择 IMAP 服务器"""
    if "@qq.com" in email or "@foxmail.com" in email:
        return "imap.qq.com"
    elif "@163.com" in email:
        return "imap.163.com"
    elif "@outlook.com" in email or "@hotmail.com" in email:
        return "outlook.office365.com"
    return "imap.gmail.com"


# 账号列表（从环境变量读取）
_email = os.getenv("EUSERV_EMAIL", "")
ACCOUNTS = [
    AccountConfig(
        email=_email,
        password=os.getenv("EUSERV_PASSWORD"),
        imap_server=get_imap_server(_email),
        email_password=os.getenv("EMAIL_PASS")
    )
    # 如需多账号，请在此处添加更多 AccountConfig 实例
]

# ======================================

def recognize_and_calculate(captcha_image_url: str, session: requests.Session) -> Optional[str]:
    """识别并计算验证码（线程安全）"""
    logger.info("正在处理验证码...")
    try:
        response = session.get(captcha_image_url)
        image_bytes = response.content
        with ocr_lock:
            text = ocr.classification(image_bytes).strip()
        logger.debug(f"OCR 识别文本: {text}")

        raw_text = text.strip()
        text = raw_text.replace(' ', '').upper()

        # 情况1：纯字母数字
        if re.fullmatch(r'[A-Z0-9]+', text):
            logger.info(f"检测到纯字母数字验证码: {raw_text}")
            return raw_text.strip()

        # 情况2：四则运算
        pattern = r'^(\d+)([+\-*/×xX÷/])(\d+|[A-Z])$'
        match = re.match(pattern, text)
        if not match:
            logger.warning(f"无法解析验证码格式: {raw_text}")
            return raw_text.strip()

        left_str, op, right_str = match.groups()
        left = int(left_str)

        if right_str.isdigit():
            right = int(right_str)
        else:
            if 'A' <= right_str <= 'Z':
                right = ord(right_str) - ord('A') + 10
            else:
                logger.warning(f"右边字符无效: {right_str}")
                return raw_text.strip()

        if op in {'*', '×', 'X', 'x'}:
            result = left * right
        elif op == '+':
            result = left + right
        elif op == '-':
            result = left - right
        elif op in {'/', '÷'}:
            if right == 0:
                logger.warning("除数为0")
                return raw_text.strip()
            if left % right != 0:
                logger.warning(f"除法非整除: {left} ÷ {right}")
                return raw_text.strip()
            result = left // right
        else:
            logger.warning(f"未知运算符: {op}")
            return raw_text.strip()

        logger.info(f"验证码计算: {left} {op} {right_str} = {result}")
        return str(result)
    except Exception as e:
        logger.error(f"验证码识别错误: {e}", exc_info=True)
        return None


def get_euserv_pin(email: str, email_password: str, imap_server: str, after_time: datetime = None, pin_type: str = 'login') -> Optional[str]:
    """从邮箱获取 EUserv PIN 码（带重试）"""
    max_retries = 12
    retry_interval = 5

    if pin_type == 'renew':
        subject_keywords = ['security check', 'confirmation']
        type_name = "续期"
    else:
        subject_keywords = ['attempted login', 'login']
        type_name = "登录"

    logger.info(f"正在从邮箱 {email} 获取{type_name} PIN 码...")
    for i in range(max_retries):
        try:
            if i > 0:
                time.sleep(retry_interval)
            with MailBox(imap_server).login(email, email_password) as mailbox:
                for msg in mailbox.fetch(limit=10, reverse=True):
                    if 'euserv' not in msg.from_.lower():
                        continue
                    if not any(k in msg.subject.lower() for k in subject_keywords):
                        continue
                    if after_time and msg.date:
                        email_dt = msg.date.replace(tzinfo=None)
                        filter_dt = after_time.replace(tzinfo=None)
                        if email_dt.timestamp() < (filter_dt.timestamp() - 120):
                            continue
                    # 提取 PIN
                    match = re.search(r'PIN:\s*\n?(\d{6})', msg.text) or re.search(r'PIN.*?(\d{6})', msg.text, re.DOTALL)
                    if match:
                        pin = match.group(1)
                        logger.info(f"✅ 提取到{type_name} PIN: {pin}")
                        return pin
                    match_fb = re.search(r'\b(\d{6})\b', msg.text)
                    if match_fb:
                        pin = match_fb.group(1)
                        logger.info(f"✅ 提取到{type_name} PIN: {pin}")
                        return pin
        except Exception as e:
            logger.warning(f"获取邮件失败: {e}")
    logger.error(f"❌ 超时未找到{type_name} PIN 码")
    return None


class EUserv:
    """EUserv 操作类"""
    def __init__(self, config: AccountConfig):
        self.config = config
        self.session = requests.Session()
        self.sess_id = None
        self._login_response_html = None

    def login(self) -> bool:
        """登录 EUserv"""
        logger.info(f"正在登录账号: {self.config.email}")
        headers = {'user-agent': USER_AGENT, 'origin': 'https://www.euserv.com'}
        url = "https://support.euserv.com/index.iphp"
        captcha_url = "https://support.euserv.com/securimage_show.php"

        try:
            # 获取 sess_id
            sess = self.session.get(url, headers=headers)
            sess_id_match = re.search(r'sess_id["\']?\s*[:=]\s*["\']?([a-zA-Z0-9]{30,100})["\']?', sess.text)
            if not sess_id_match:
                sess_id_match = re.search(r'sess_id=([a-zA-Z0-9]{30,100})', sess.text)
            if not sess_id_match:
                logger.error("❌ 无法获取 sess_id")
                return False
            sess_id = sess_id_match.group(1)
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
            response = self.session.post(url, headers=headers, data=login_data)
            response.raise_for_status()

            # 检查错误
            if 'Please check email address/customer ID and password' in response.text:
                logger.error("❌ 用户名或密码错误")
                return False
            if 'kc2_login_iplock_cdown' in response.text:
                logger.error("❌ 账号被锁定，请5分钟后重试")
                return False

            # 处理验证码
            if 'captcha' in response.text.lower():
                captcha_success = False
                for attempt in range(3):
                    logger.info(f"验证码识别尝试 {attempt+1}/3")
                    code = recognize_and_calculate(captcha_url, self.session)
                    if not code:
                        continue
                    captcha_data = {
                        'subaction': 'login',
                        'sess_id': sess_id,
                        'captcha_code': code
                    }
                    resp = self.session.post(url, headers=headers, data=captcha_data)
                    resp.raise_for_status()
                    if 'captcha' not in resp.text.lower():
                        captcha_success = True
                        response = resp
                        logger.info("✅ 验证码通过")
                        break
                if not captcha_success:
                    logger.error("❌ 验证码连续失败")
                    return False

            # 处理 PIN 验证
            if 'PIN that you receive via email' in response.text:
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
                    logger.error("❌ 获取 PIN 失败")
                    return False
                soup = BeautifulSoup(response.text, "html.parser")
                confirm_data = {
                    'pin': pin,
                    'sess_id': sess_id,
                    'Submit': 'Confirm',
                    'subaction': 'login',
                    'c_id': soup.find("input", {"name": "c_id"})["value"],
                }
                response = self.session.post(url, headers=headers, data=confirm_data)
                response.raise_for_status()

            # 检查登录成功
            if any(key in response.text for key in ['Hello', 'Confirm or change your customer data', 'logout']):
                logger.info(f"✅ 账号 {self.config.email} 登录成功")
                self.sess_id = sess_id
                self._login_response_html = response.text
                return True
            else:
                logger.error(f"❌ 登录失败")
                return False
        except Exception as e:
            logger.error(f"登录异常: {e}", exc_info=True)
            return False

    def confirm_customer_data(self) -> bool:
        """确认 Customer Data 页面"""
        if not self.sess_id:
            return False
        url = "https://support.euserv.com/index.iphp"
        headers = {'user-agent': USER_AGENT, 'origin': 'https://support.euserv.com', 'Referer': url}
        try:
            # 获取页面
            page_html = getattr(self, '_login_response_html', None)
            if not page_html:
                resp = self.session.get(f"{url}?sess_id={self.sess_id}&subaction=show_kc2_customer_customer_data", headers=headers)
                resp.raise_for_status()
                page_html = resp.text

            if 'must be checked and confirmed' not in page_html:
                logger.info("✓ Customer Data 无需确认")
                return True

            logger.info("⚠️ 检测到 Customer Data 需要确认，正在自动提交...")
            soup = BeautifulSoup(page_html, 'html.parser')
            target_form = None
            for form in soup.find_all('form'):
                if form.find('input', {'value': re.compile(r'Save', re.I)}):
                    target_form = form
                    break
            if not target_form:
                logger.warning("未找到表单")
                return False

            # 收集表单数据
            form_data = []
            phone_prefix = fax_prefix = ''
            for inp in target_form.find_all('input'):
                name = inp.get('name')
                if not name or inp.get('disabled') is not None:
                    continue
                typ = inp.get('type', '').lower()
                if typ == 'checkbox':
                    if inp.get('checked') is not None:
                        form_data.append((name, inp.get('value', 'on')))
                elif typ == 'radio':
                    if inp.get('checked') is not None:
                        form_data.append((name, inp.get('value', '')))
                elif typ == 'submit':
                    if inp.get('value', '').lower() in ('save', 'speichern'):
                        form_data.append((name, inp.get('value', 'Save')))
                else:
                    value = inp.get('value', '')
                    form_data.append((name, value))
                    if name == 'c_phone_country_prefix':
                        phone_prefix = value
                    elif name == 'c_fax_country_prefix':
                        fax_prefix = value

            for sel in target_form.find_all('select'):
                name = sel.get('name')
                if not name or sel.get('disabled') is not None:
                    continue
                selected = sel.find('option', selected=True)
                if selected:
                    val = selected.get('value', selected.get_text(strip=True))
                else:
                    first = sel.find('option')
                    val = first.get('value', first.get_text(strip=True)) if first else ''
                form_data.append((name, val))

            if phone_prefix:
                form_data.append(('form_c_phone_country_prefix', phone_prefix))
            if fax_prefix:
                form_data.append(('form_c_fax_country_prefix', fax_prefix))

            # 替换 sess_id
            form_data = [(k, v) if k != 'sess_id' else (k, self.sess_id) for k, v in form_data]

            action = target_form.get('action', '')
            submit_url = f"https://support.euserv.com/{action.lstrip('/')}" if action and not action.startswith('http') else (action or url)
            resp = self.session.post(submit_url, headers=headers, data=form_data)
            resp.raise_for_status()

            # 判断结果
            try:
                res_json = resp.json()
                if res_json.get('rc') == '100':
                    logger.info("✅ Customer Data 确认成功")
                    return True
                else:
                    logger.warning(f"确认返回非成功: {res_json}")
                    return False
            except:
                if 'must be checked and confirmed' not in resp.text:
                    logger.info("✅ Customer Data 确认成功")
                    return True
                logger.warning("确认后仍有警告")
                return False
        except Exception as e:
            logger.error(f"Customer Data 确认异常: {e}", exc_info=True)
            return False

    def get_servers(self) -> Dict[str, Tuple[bool, str]]:
        """获取服务器列表"""
        if not self.sess_id:
            return {}
        url = f"https://support.euserv.com/index.iphp?sess_id={self.sess_id}"
        headers = {'user-agent': USER_AGENT}
        try:
            resp = self.session.get(url, headers=headers)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, 'html.parser')
            servers = {}
            selector = '#kc2_order_customer_orders_tab_content_1 .kc2_order_table.kc2_content_table tr, #kc2_order_customer_orders_tab_content_2 .kc2_order_table.kc2_content_table tr'
            for tr in soup.select(selector):
                sid_td = tr.select('.td-z1-sp1-kc')
                if len(sid_td) != 1:
                    continue
                sid = sid_td[0].get_text().strip()
                row_text = tr.get_text().lower()
                if sid in SKIP_CONTRACTS:
                    logger.info(f"⏭️ 跳过合同 {sid}")
                    continue
                if 'sync' in row_text and 'share' in row_text:
                    logger.info(f"⏭️ 跳过 Sync & Share 合同 {sid}")
                    continue
                action_container = tr.select('.td-z1-sp2-kc .kc2_order_action_container')
                if not action_container:
                    continue
                action_text = action_container[0].get_text()
                can_renew = 'Contract extension possible from' not in action_text
                renew_date = ''
                if not can_renew:
                    date_match = re.search(r'\b(\d{4}-\d{2}-\d{2})\b', action_text)
                    if date_match:
                        renew_date = date_match.group(1)
                        can_renew = datetime.now().date() >= datetime.strptime(renew_date, "%Y-%m-%d").date()
                servers[sid] = (can_renew, renew_date)
            logger.info(f"✅ 账号 {self.config.email} 找到 {len(servers)} 台服务器")
            return servers
        except Exception as e:
            logger.error(f"获取服务器列表失败: {e}", exc_info=True)
            return {}

    def renew_server(self, order_id: str) -> bool:
        """续期指定服务器"""
        logger.info(f"正在续期服务器 {order_id}...")
        url = "https://support.euserv.com/index.iphp"
        headers = {'user-agent': USER_AGENT, 'origin': 'https://support.euserv.com', 'Referer': url}

        try:
            # 步骤1：选择订单
            data1 = {
                'Submit': 'Extend contract',
                'sess_id': self.sess_id,
                'ord_no': order_id,
                'subaction': 'choose_order',
                'show_contract_extension': '1',
                'choose_order_subaction': 'show_contract_details'
            }
            resp1 = self.session.post(url, headers=headers, data=data1)
            resp1.raise_for_status()

            # 步骤2：触发发送 PIN
            pin_time = datetime.now()
            data2 = {
                'sess_id': self.sess_id,
                'subaction': 'show_kc2_security_password_dialog',
                'prefix': 'kc2_customer_contract_details_extend_contract_',
                'type': '1'
            }
            resp2 = self.session.post(url, headers=headers, data=data2)
            resp2.raise_for_status()

            # 步骤3：获取 PIN
            time.sleep(5)
            pin = get_euserv_pin(
                self.config.email,
                self.config.email_password,
                self.config.imap_server,
                after_time=pin_time,
                pin_type='renew'
            )
            if not pin:
                logger.error("❌ 获取续期 PIN 失败")
                return False

            # 步骤4：获取 token
            data3 = {
                'sess_id': self.sess_id,
                'auth': pin,
                'subaction': 'kc2_security_password_get_token',
                'prefix': 'kc2_customer_contract_details_extend_contract_',
                'type': '1',
                'ident': f'kc2_customer_contract_details_extend_contract_{order_id}'
            }
            resp3 = self.session.post(url, headers=headers, data=data3)
            resp3.raise_for_status()
            result3 = resp3.json()
            if result3.get('rs') != 'success':
                logger.error(f"获取 token 失败: {result3}")
                return False
            token = result3['token']['value']

            # 步骤5：获取确认对话框
            data4 = {
                'sess_id': self.sess_id,
                'subaction': 'kc2_customer_contract_details_get_extend_contract_confirmation_dialog',
                'token': token
            }
            resp4 = self.session.post(url, headers=headers, data=data4)
            resp4.raise_for_status()

            # 解析对话框，提取 hidden 字段
            dialog_html = ''
            try:
                j4 = resp4.json()
                if isinstance(j4, dict):
                    dialog_html = j4.get('html', {}).get('value', '') or j4.get('value', '')
            except:
                dialog_html = resp4.text

            # 提取 subaction
            subaction_match = re.search(r'name=["\']subaction["\']\s+value=["\']([^"\']+)["\']', dialog_html)
            next_subaction = subaction_match.group(1) if subaction_match else 'kc2_customer_contract_details_extend_contract_term'

            # 提取所有 hidden
            data_confirm = {}
            for match in re.finditer(r'<input[^>]+type=["\']hidden["\'][^>]*>', dialog_html, re.IGNORECASE):
                tag = match.group(0)
                name_m = re.search(r'name=["\']([^"\']+)["\']', tag)
                val_m = re.search(r'value=["\']([^"\']*)["\']', tag)
                if name_m and val_m:
                    data_confirm[name_m.group(1)] = val_m.group(1)

            # 确保 token 存在
            if 'token' not in data_confirm:
                token_m = re.search(r'name=["\']token["\']\s+value=["\']([^"\']+)["\']', dialog_html)
                if token_m:
                    data_confirm['token'] = token_m.group(1)

            # 步骤6：提交续期
            time.sleep(2)
            resp5 = self.session.post(url, headers=headers, data=data_confirm)
            resp5.raise_for_status()

            # 判断结果
            html_lower = resp5.text.lower()
            if "error: token missing" in html_lower:
                logger.error("❌ 续期失败: token missing")
                return False
            success_keywords = ['successfully extended', 'erfolgreich', 'contract extended', 'verlängert', 'extension successful']
            for kw in success_keywords:
                if kw in html_lower:
                    logger.info(f"✅ 服务器 {order_id} 续期成功")
                    return True
            logger.info(f"✅ 服务器 {order_id} 续期请求已提交（请检查邮件）")
            return True
        except Exception as e:
            logger.error(f"❌ 服务器 {order_id} 续期异常: {e}", exc_info=True)
            return False


# ---------- 通知发送函数 ----------
def send_telegram(message: str, config: GlobalConfig):
    if not config.telegram_bot_token or not config.telegram_chat_id:
        return
    url = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"
    data = {"chat_id": config.telegram_chat_id, "text": message, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=data, timeout=10)
        if resp.status_code == 200:
            logger.info("✅ Telegram 通知发送成功")
        else:
            logger.error(f"❌ Telegram 失败: {resp.status_code}")
    except Exception as e:
        logger.error(f"❌ Telegram 异常: {e}")


def send_wechat(message: str):
    """发送微信通知（接口要求 content、title、token 三个参数）"""
    api_url = os.getenv("WECHAT_API_URL")
    auth_token = os.getenv("WECHAT_AUTH_TOKEN")
    if not api_url or not auth_token:
        return

    payload = {
        "content": message,
        "title": "EUserv 续期通知",      # 固定标题
        "token": auth_token              # 接口要求的 token 字段
    }
    headers = {"Content-Type": "application/json"}
    try:
        resp = requests.post(api_url, json=payload, headers=headers, timeout=10)
        if resp.status_code == 200:
            logger.info("✅ 微信通知发送成功")
        else:
            logger.error(f"❌ 微信失败: {resp.status_code} - {resp.text}")
    except Exception as e:
        logger.error(f"❌ 微信异常: {e}", exc_info=True)


# ---------- 账号处理函数 ----------
def process_account(account_config: AccountConfig, global_config: GlobalConfig) -> Dict:
    result = {
        'email': account_config.email,
        'success': False,
        'servers': {},
        'renew_results': [],
        'error': None
    }
    try:
        euserv = EUserv(account_config)
        login_ok = False
        for attempt in range(global_config.max_login_retries):
            if attempt > 0:
                time.sleep(5)
            if euserv.login():
                login_ok = True
                break
        if not login_ok:
            result['error'] = "登录失败"
            return result

        euserv.confirm_customer_data()
        servers = euserv.get_servers()
        result['servers'] = servers
        if not servers:
            result['error'] = "未找到任何服务器"
            result['success'] = True
            return result

        # 只处理第一个可续期合同
        for order_id, (can_renew, renew_date) in servers.items():
            if can_renew:
                logger.info(f"⏰ 服务器 {order_id} 可以续期")
                success = euserv.renew_server(order_id)
                if success:
                    # 续期成功，下次可续期日期为当前日期 + 365天
                    next_date = (datetime.now() + timedelta(days=365)).strftime("%Y-%m-%d")
                    result['renew_results'].append({
                        'order_id': order_id,
                        'success': True,
                        'message': f"✅ 服务器 {order_id} 续期成功",
                        'next_renew_date': next_date
                    })
                else:
                    # 续期失败，保留原有可续期日期（可能为空）
                    result['renew_results'].append({
                        'order_id': order_id,
                        'success': False,
                        'message': f"❌ 服务器 {order_id} 续期失败",
                        'next_renew_date': renew_date
                    })
                break
        result['success'] = True
    except Exception as e:
        logger.error(f"处理账号异常: {e}", exc_info=True)
        result['error'] = str(e)
    return result


def main():
    logger.info("=" * 60)
    logger.info("EUserv 多账号自动续期脚本（多线程版本）")
    logger.info(f"执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"配置账号数: {len(ACCOUNTS)}")
    logger.info(f"最大并发: {GLOBAL_CONFIG.max_workers}")
    logger.info("=" * 60)

    if not ACCOUNTS:
        logger.error("❌ 未配置账号")
        sys.exit(1)

    all_results = []
    with ThreadPoolExecutor(max_workers=GLOBAL_CONFIG.max_workers) as executor:
        futures = {executor.submit(process_account, acc, GLOBAL_CONFIG): acc for acc in ACCOUNTS}
        for future in as_completed(futures):
            acc = futures[future]
            try:
                res = future.result()
                all_results.append(res)
            except Exception as e:
                logger.error(f"账号 {acc.email} 处理异常: {e}")
                all_results.append({'email': acc.email, 'success': False, 'error': str(e)})

    # 生成两套通知消息：带 HTML 用于 Telegram，纯文本用于微信
    tg_lines = [f"<b>🔄 EUserv 多账号续期报告</b>", f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", f"处理账号数: {len(all_results)}", ""]
    wx_lines = [f"🔄 EUserv 多账号续期报告", f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", f"处理账号数: {len(all_results)}", ""]

    for res in all_results:
        email = res['email']
        if not res['success']:
            err = res.get('error', '未知错误')
            tg_lines.append(f"<b>❌ 账号: {email}</b>")
            tg_lines.append(f"   登录失败: {err}")
            wx_lines.append(f"❌ 账号: {email}")
            wx_lines.append(f"   登录失败: {err}")
        elif res.get('renew_results'):
            tg_lines.append(f"<b>🎉 账号: {email}</b>")
            tg_lines.append(f"   续期结果:")
            wx_lines.append(f"🎉 账号: {email}")
            wx_lines.append(f"   续期结果:")
            for item in res['renew_results']:
                tg_lines.append(f"   {item['message']}")
                wx_lines.append(f"   {item['message']}")
                if item.get('next_renew_date'):
                    tg_lines.append(f"         下次续期: {item['next_renew_date']}")
                    wx_lines.append(f"         下次续期: {item['next_renew_date']}")
        else:
            tg_lines.append(f"<b>✅ 账号: {email}</b>")
            tg_lines.append(f"   所有服务器无需续期")
            wx_lines.append(f"✅ 账号: {email}")
            wx_lines.append(f"   所有服务器无需续期")
            for oid, (_, rdate) in res.get('servers', {}).items():
                if rdate:
                    tg_lines.append(f"    订单 {oid}: 可续期日期 {rdate}")
                    wx_lines.append(f"    订单 {oid}: 可续期日期 {rdate}")
        tg_lines.append("")
        wx_lines.append("")

    tg_msg = "\n".join(tg_lines)
    wx_msg = "\n".join(wx_lines)

    # 发送通知
    send_telegram(tg_msg, GLOBAL_CONFIG)
    send_wechat(wx_msg)

    logger.info("\n" + "=" * 60)
    logger.info("执行完成")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
