#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EUserv 自动续期脚本 - 多账号多线程版本
支持多账号配置、多线程并发处理、自动登录、验证码识别、检查到期状态、自动续期并发送 Telegram 通知

ARM64 / Alpine Docker 适配版本：
  - 使用 pytesseract + Pillow 替代 ddddocr（避免 onnxruntime 在 musl/aarch64 上无法安装的问题）
  - 系统依赖：apk add --no-cache tesseract-ocr tesseract-ocr-data-eng
  - Python 依赖：pip3 install Pillow requests beautifulsoup4 imap-tools pytesseract
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

from PIL import Image, ImageFilter, ImageEnhance, ImageOps
import pytesseract
import requests
from bs4 import BeautifulSoup
from imap_tools import MailBox

# ============================================================
# 日志配置
# DEBUG 模式：
#   - 不设置或 DEBUG=false : 普通模式（INFO 级别日志）
#   - DEBUG=true            : 调试模式（DEBUG 级别日志）
#   - DEBUG=html            : 调试 + HTML 保存模式
# ============================================================
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

# ============================================================
# 环境变量：要跳过的合同 ID（逗号分隔）
# 例如: SKIP_CONTRACTS=475282,123456
# ============================================================
SKIP_CONTRACTS = [x.strip() for x in os.getenv("SKIP_CONTRACTS", "").split(",") if x.strip()]
if SKIP_CONTRACTS:
    logger.info(f"配置了跳过合同列表: {SKIP_CONTRACTS}")

# 兼容新版 Pillow（>=10.0.0 移除了 ANTIALIAS）
if not hasattr(Image, 'ANTIALIAS'):
    Image.ANTIALIAS = Image.Resampling.LANCZOS

# NOTE: pytesseract 本身线程安全，但加锁避免并发写 tesseract 临时文件冲突
ocr_lock = threading.Lock()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/94.0.4606.61 Safari/537.36"
)


# ============================================================
# 配置数据类
# ============================================================
class AccountConfig:
    """单个账号配置"""
    def __init__(self, email: str, password: str,
                 imap_server: str = 'imap.gmail.com',
                 email_password: str = ''):
        self.email = email
        self.password = password
        self.imap_server = imap_server
        # 若未单独设置邮箱密码，则复用登录密码
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


# ============================================================
# 全局配置实例
# ============================================================
GLOBAL_CONFIG = GlobalConfig(
    telegram_bot_token=os.getenv("TG_BOT_TOKEN", ""),
    telegram_chat_id=os.getenv("TG_CHAT_ID", ""),
    max_workers=3,        # 建议不超过 5，避免触发频率限制
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


# ============================================================
# 账号列表（从环境变量读取）
# ============================================================
_email = os.getenv("EUSERV_EMAIL", "")
ACCOUNTS = [
    AccountConfig(
        email=_email,
        password=os.getenv("EUSERV_PASSWORD", ""),
        imap_server=get_imap_server(_email),
        email_password=os.getenv("EMAIL_PASS", "")
    ),
    # 添加更多账号示例：
    # AccountConfig(
    #     email="account2@gmail.com",
    #     password="password2",
    #     imap_server="imap.gmail.com",
    #     email_password="app_specific_password2"
    # ),
]


# ============================================================
# OCR：使用 pytesseract 多策略识别（替代 ddddocr，支持 Alpine/ARM64）
# NOTE: tesseract 并非专为验证码设计，采用多种预处理策略提升识别准确率，
#       选择字符数最多的结果作为最终输出——以量取胜。
# ============================================================

# tesseract 字符白名单（仅允许验证码常见字符）
_TESS_WHITELIST = (
    '0123456789'
    'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
    '+-*/='
)


def _add_border(img: Image.Image, size: int = 20, color: int = 255) -> Image.Image:
    """为图片添加白色边框，防止边缘字符被裁切"""
    bordered = Image.new('L', (img.width + size * 2, img.height + size * 2), color)
    bordered.paste(img, (size, size))
    return bordered


def _tesseract_ocr(img: Image.Image, psm: int = 7) -> str:
    """调用 tesseract 执行 OCR（加锁保证线程安全）"""
    config = f'--psm {psm} -c tessedit_char_whitelist={_TESS_WHITELIST}'
    with ocr_lock:
        return pytesseract.image_to_string(img, config=config).strip()


def _preprocess_strategies(raw_img: Image.Image) -> list:
    """
    生成多种预处理后的图片列表，每种策略针对不同类型的验证码噪点。
    返回 [(策略名, 处理后图片), ...]
    """
    w, h = raw_img.size
    results = []

    # 策略 1：放大 4 倍 + 对比度增强 + 锐化（适合低对比度验证码）
    img1 = raw_img.resize((w * 4, h * 4), Image.LANCZOS)
    img1 = ImageEnhance.Contrast(img1).enhance(3.0)
    img1 = img1.filter(ImageFilter.SHARPEN)
    img1 = _add_border(img1)
    results.append(('放大4x+对比度3.0', img1))

    # 策略 2：放大 3 倍 + 固定阈值二值化 128（适合中等噪点）
    img2 = raw_img.resize((w * 3, h * 3), Image.LANCZOS)
    img2 = img2.point(lambda p: 255 if p > 128 else 0, '1').convert('L')
    img2 = _add_border(img2)
    results.append(('放大3x+阈值128', img2))

    # 策略 3：放大 4 倍 + 中值滤波去噪 + 低阈值二值化（适合干扰线多的验证码）
    img3 = raw_img.resize((w * 4, h * 4), Image.LANCZOS)
    img3 = img3.filter(ImageFilter.MedianFilter(size=3))
    img3 = img3.point(lambda p: 255 if p > 100 else 0, '1').convert('L')
    img3 = _add_border(img3)
    results.append(('放大4x+中值滤波+阈值100', img3))

    # 策略 4：放大 5 倍 + 反色 + 高阈值（适合深色背景浅色字体的验证码）
    img4 = raw_img.resize((w * 5, h * 5), Image.LANCZOS)
    img4 = ImageOps.invert(img4)
    img4 = img4.point(lambda p: 255 if p > 150 else 0, '1').convert('L')
    img4 = _add_border(img4)
    results.append(('放大5x+反色+阈值150', img4))

    # 策略 5：放大 3 倍 + 高斯模糊后锐化 + 高对比度（适合文字模糊的验证码）
    img5 = raw_img.resize((w * 3, h * 3), Image.LANCZOS)
    img5 = img5.filter(ImageFilter.GaussianBlur(radius=1))
    img5 = ImageEnhance.Contrast(img5).enhance(4.0)
    img5 = img5.filter(ImageFilter.SHARPEN)
    img5 = _add_border(img5)
    results.append(('放大3x+模糊+对比度4.0', img5))

    return results


def _ocr_image_bytes(image_bytes: bytes) -> str:
    """
    对图片字节流执行多策略 OCR 识别。
    尝试 5 种不同的图像预处理策略，对每种结果评分，
    选择识别字符数最多（得分最高）的结果返回。
    """
    raw_img = Image.open(io.BytesIO(image_bytes)).convert('L')

    strategies = _preprocess_strategies(raw_img)
    best_text = ''
    best_score = -1
    best_strategy = ''

    for psm in [7, 8, 13]:  # 尝试多种 psm 模式
        for strategy_name, processed_img in strategies:
            try:
                text = _tesseract_ocr(processed_img, psm=psm)
                # 只保留白名单中的字符
                cleaned = ''.join(c for c in text if c in _TESS_WHITELIST)
                score = len(cleaned)
                logger.debug(
                    f"  策略[{strategy_name}|psm{psm}] -> "
                    f"原始={text!r}, 清洗={cleaned!r}, 得分={score}"
                )
                if score > best_score:
                    best_score = score
                    best_text = cleaned
                    best_strategy = f'{strategy_name}|psm{psm}'
            except Exception as e:
                logger.debug(f"  策略[{strategy_name}|psm{psm}] 异常: {e}")

    logger.info(f"OCR 最佳结果: {best_text!r} (策略: {best_strategy}, 得分: {best_score})")
    return best_text


def recognize_and_calculate(captcha_image_url: str, session: requests.Session) -> Optional[str]:
    """
    下载验证码图片并识别，若为数学运算式则自动计算结果后返回。
    支持：纯字母数字 / 加减乘除运算式（含字母数字混合右操作数）
    """
    logger.info("正在处理验证码...")
    try:
        response = session.get(captcha_image_url)
        image_bytes = response.content

        text = _ocr_image_bytes(image_bytes)
        logger.debug(f"OCR 识别原始文本: {text!r}")

        raw_text = text.strip()
        text_upper = raw_text.replace(' ', '').upper()

        # 验证码通常至少 3 个字符，太短说明识别失败
        if len(text_upper) < 3:
            logger.warning(f"识别结果过短（{len(text_upper)} 字符），可能识别失败: {raw_text!r}")
            return None  # 返回 None 触发重试

        # 情况 1：纯字母数字，直接返回
        if re.fullmatch(r'[A-Z0-9]+', text_upper):
            logger.info(f"检测到纯字母数字验证码: {raw_text}")
            return raw_text.strip()

        # 情况 2：四则运算式
        # 支持运算符：+ - * × x X / ÷
        pattern = r'^(\d+)([+\-*/×xX÷])(\d+|[A-Z])$'
        match = re.match(pattern, text_upper)
        if not match:
            logger.warning(f"无法解析验证码格式，原样返回: {raw_text!r}")
            return raw_text.strip()

        left_str, op, right_str = match.groups()
        left = int(left_str)

        # 右操作数：纯数字 or 字母（A=10, B=11, ...）
        if right_str.isdigit():
            right = int(right_str)
        elif 'A' <= right_str <= 'Z':
            right = ord(right_str) - ord('A') + 10
        else:
            logger.warning(f"右操作数无效: {right_str}")
            return raw_text.strip()

        if op in {'*', '×', 'X', 'x'}:
            result, op_name = left * right, '乘'
        elif op == '+':
            result, op_name = left + right, '加'
        elif op == '-':
            result, op_name = left - right, '减'
        elif op in {'/', '÷'}:
            if right == 0:
                logger.warning("除数为 0，无法计算")
                return raw_text.strip()
            if left % right != 0:
                logger.warning(f"除法非整除: {left} ÷ {right} = {left / right:.2f}")
                return raw_text.strip()
            result, op_name = left // right, '除'
        else:
            logger.warning(f"未知运算符: {op}")
            return raw_text.strip()

        logger.info(f"验证码计算: {left} {op_name} {right_str} = {result}")
        return str(result)

    except Exception as e:
        logger.error(f"验证码识别出现异常: {e}", exc_info=True)
        return None


# ============================================================
# 邮箱 PIN 获取
# ============================================================
def get_euserv_pin(
    email: str,
    email_password: str,
    imap_server: str,
    after_time: datetime = None,
    pin_type: str = 'login'
) -> Optional[str]:
    """
    从邮箱获取 EUserv PIN 码（带重试机制）

    Args:
        after_time: 只查找此时间之后收到的邮件，用于区分新旧 PIN
        pin_type  : 'login' 登录 PIN / 'renew' 续期 PIN
    """
    max_retries = 12
    retry_interval = 5  # 秒，总等待约 60 秒

    if pin_type == 'renew':
        subject_keywords = ['security check', 'confirmation']
        type_name = "续期"
    else:
        subject_keywords = ['attempted login', 'login']
        type_name = "登录"

    logger.info(
        f"正在从邮箱 {email} 获取{type_name} PIN 码 "
        f"(最长等待 {max_retries * retry_interval} 秒)..."
    )
    if after_time:
        logger.debug(f"只查找 {after_time.strftime('%H:%M:%S')} 之后的邮件")

    for i in range(max_retries):
        try:
            if i > 0:
                logger.info(f"第 {i + 1} 次尝试获取邮件...")
                time.sleep(retry_interval)

            with MailBox(imap_server).login(email, email_password) as mailbox:
                for msg in mailbox.fetch(limit=10, reverse=True):
                    if 'euserv' not in msg.from_.lower():
                        continue

                    subject_lower = msg.subject.lower()
                    if not any(kw in subject_lower for kw in subject_keywords):
                        continue

                    # 时间过滤（允许 2 分钟误差）
                    if after_time and msg.date:
                        email_dt = msg.date
                        if email_dt.tzinfo is None:
                            email_dt = email_dt.replace(
                                tzinfo=datetime.now().astimezone().tzinfo
                            )
                        filter_dt = after_time
                        if filter_dt.tzinfo is None:
                            filter_dt = filter_dt.replace(
                                tzinfo=datetime.now().astimezone().tzinfo
                            )
                        if email_dt.timestamp() < (filter_dt.timestamp() - 120):
                            continue

                    # 提取 PIN（优先精确匹配，备用宽松匹配）
                    text = msg.text or ""
                    match = (
                        re.search(r'PIN:\s*\n?(\d{6})', text)
                        or re.search(r'PIN.*?(\d{6})', text, re.DOTALL)
                    )
                    if match:
                        pin = match.group(1)
                        logger.info(f"✅ 提取到{type_name} PIN 码: {pin}")
                        return pin

                    fallback = re.search(r'\b(\d{6})\b', text)
                    if fallback:
                        pin = fallback.group(1)
                        logger.info(f"✅ 提取到{type_name} PIN 码（备用）: {pin}")
                        return pin

        except Exception as e:
            logger.warning(f"获取邮件尝试失败: {e}")

    logger.error(f"❌ 超时未找到{type_name} PIN 码邮件")
    return None


# ============================================================
# EUserv 操作类
# ============================================================
class EUserv:
    """EUserv 账号操作封装"""

    def __init__(self, config: AccountConfig):
        self.config = config
        self.session = requests.Session()
        self.sess_id: Optional[str] = None
        self._login_response_html: str = ""

    def login(self) -> bool:
        """登录 EUserv（支持验证码和邮件 PIN 两步验证）"""
        logger.info(f"正在登录账号: {self.config.email}")

        headers = {
            'user-agent': USER_AGENT,
            'origin': 'https://www.euserv.com'
        }
        url = "https://support.euserv.com/index.iphp"
        captcha_url = "https://support.euserv.com/securimage_show.php"

        try:
            # 获取 sess_id
            sess = self.session.get(url, headers=headers)
            sess_id_match = re.search(
                r'sess_id["\']?\s*[:=]\s*["\']?([a-zA-Z0-9]{30,100})["\']?',
                sess.text
            ) or re.search(r'sess_id=([a-zA-Z0-9]{30,100})', sess.text)

            if not sess_id_match:
                logger.error("❌ 无法获取 sess_id")
                return False

            sess_id = sess_id_match.group(1)
            logger.debug(f"获取到 sess_id: {sess_id[:20]}...")

            self.session.get("https://support.euserv.com/pic/logo_small.png", headers=headers)

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

            if 'Please check email address/customer ID and password' in response.text:
                logger.error("❌ 用户名或密码错误")
                return False
            if 'kc2_login_iplock_cdown' in response.text:
                logger.error("❌ 密码错误次数过多，账号被锁定，请 5 分钟后重试")
                return False

            # 处理验证码（最多重试 3 次）
            if 'captcha' in response.text.lower():
                captcha_max_retries = 3
                captcha_success = False

                for captcha_attempt in range(captcha_max_retries):
                    logger.info(f"⚠️ 需要验证码，正在识别... (第 {captcha_attempt + 1}/{captcha_max_retries} 次)")
                    captcha_code = recognize_and_calculate(captcha_url, self.session)

                    if not captcha_code:
                        logger.warning(f"验证码识别失败 (第 {captcha_attempt + 1} 次)")
                        if captcha_attempt < captcha_max_retries - 1:
                            time.sleep(2)
                        continue

                    response = self.session.post(url, headers=headers, data={
                        'subaction': 'login',
                        'sess_id': sess_id,
                        'captcha_code': captcha_code
                    })
                    response.raise_for_status()

                    if 'captcha' not in response.text.lower():
                        captcha_success = True
                        logger.info("✅ 验证码通过")
                        break
                    else:
                        logger.warning(f"验证码错误 (第 {captcha_attempt + 1} 次)")
                        if captcha_attempt < captcha_max_retries - 1:
                            time.sleep(2)

                if not captcha_success:
                    logger.error(f"❌ 验证码连续 {captcha_max_retries} 次失败，放弃登录")
                    return False

            # 处理邮件 PIN 二步验证
            if 'PIN that you receive via email' in response.text:
                logger.info("⚠️ 需要 PIN 验证")
                pin_request_time = datetime.now()
                time.sleep(3)

                pin = get_euserv_pin(
                    self.config.email,
                    self.config.email_password,
                    self.config.imap_server,
                    after_time=pin_request_time,
                    pin_type='login'
                )
                if not pin:
                    logger.error("❌ 获取 PIN 码失败")
                    return False

                soup = BeautifulSoup(response.text, "html.parser")
                c_id_input = soup.find("input", {"name": "c_id"})
                response = self.session.post(url, headers=headers, data={
                    'pin': pin,
                    'sess_id': sess_id,
                    'Submit': 'Confirm',
                    'subaction': 'login',
                    'c_id': c_id_input["value"] if c_id_input else "",
                })
                response.raise_for_status()

            # 判断登录成功
            success = any([
                'Hello' in response.text,
                'Confirm or change your customer data here' in response.text,
                'logout' in response.text.lower() and 'customer' in response.text.lower()
            ])

            if success:
                logger.info(f"✅ 账号 {self.config.email} 登录成功")
                self.sess_id = sess_id
                self._login_response_html = response.text
                return True
            else:
                logger.error(f"❌ 账号 {self.config.email} 登录失败")
                return False

        except Exception as e:
            logger.error(f"❌ 登录过程出现异常: {e}", exc_info=True)
            return False

    def confirm_customer_data(self) -> bool:
        """确认 Customer Data 页面，解除面板功能限制

        登录后 EUserv 会显示 Customer Data 页面，要求用户确认个人信息。
        如果不确认，部分面板功能（包括续期）可能受限。
        此方法自动解析表单并提交，相当于点击 "Save" 按钮。

        NOTE: 表单中有数组字段 (如 c_birthday[], c_phone[])，同一个 name 有多个 input，
              必须使用 list of tuples 格式提交 POST 数据，否则同名字段会被覆盖。
        """
        if not self.sess_id:
            logger.warning("⚠️ 未登录，跳过 Customer Data 确认")
            return False

        url = "https://support.euserv.com/index.iphp"
        headers = {
            'user-agent': USER_AGENT,
            'origin': 'https://support.euserv.com',
            'Referer': 'https://support.euserv.com/index.iphp'
        }

        try:
            # 优先使用登录后返回的 HTML，避免额外请求
            page_html = self._login_response_html

            if not page_html:
                logger.debug("没有缓存的登录页面，主动请求 Customer Data 页面...")
                resp = self.session.get(
                    f"{url}?sess_id={self.sess_id}&subaction=show_kc2_customer_customer_data",
                    headers=headers
                )
                resp.raise_for_status()
                page_html = resp.text

            need_confirm_indicators = [
                'must be checked and confirmed',
                'Confirm or change your customer data here',
            ]

            if not any(indicator in page_html for indicator in need_confirm_indicators):
                logger.info("✓ Customer Data 页面无需确认，跳过")
                return True

            logger.info("⚠️ 检测到 Customer Data 需要确认，正在自动提交...")

            if SAVE_HTML_MODE:
                try:
                    with open('customer_data_page.html', 'w', encoding='utf-8') as f:
                        f.write(page_html)
                    logger.debug("已保存 customer_data_page.html")
                except Exception as e:
                    logger.warning(f"保存 customer_data_page.html 失败: {e}")

            soup = BeautifulSoup(page_html, 'html.parser')

            # 查找包含 "Save" 按钮的表单
            target_form = None
            for form in soup.find_all('form'):
                has_save = form.find('input', {'value': re.compile(r'Save', re.I)})
                if has_save:
                    target_form = form
                    logger.debug(f"找到目标表单 (action={form.get('action', 'N/A')})")
                    break

            # 备用：查找含 customer_data 相关 subaction 的表单
            if not target_form:
                for form in soup.find_all('form'):
                    subaction_inp = form.find('input', {'name': 'subaction'})
                    if subaction_inp and 'customer_data' in subaction_inp.get('value', ''):
                        target_form = form
                        break

            if not target_form:
                logger.warning("⚠️ 未找到 Customer Data 表单，跳过确认")
                return False

            # NOTE: 使用 list of tuples 格式，支持同名数组字段
            form_data = []
            phone_prefix = ''
            fax_prefix = ''

            for inp in target_form.find_all('input'):
                name = inp.get('name')
                if not name:
                    continue
                # 浏览器不提交 disabled 字段
                if inp.get('disabled') is not None:
                    logger.debug(f"跳过 disabled 字段: {name}")
                    continue

                input_type = inp.get('type', 'text').lower()

                if input_type == 'checkbox':
                    if inp.get('checked') is not None:
                        form_data.append((name, inp.get('value', 'on')))
                elif input_type == 'radio':
                    if inp.get('checked') is not None:
                        form_data.append((name, inp.get('value', '')))
                elif input_type == 'submit':
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
                    form_data.append((name, selected.get('value', selected.get_text(strip=True))))
                else:
                    first_opt = sel.find('option')
                    if first_opt:
                        form_data.append((name, first_opt.get('value', first_opt.get_text(strip=True))))

            # 模拟 JavaScript 动态注入的国家前缀字段
            if phone_prefix:
                form_data.append(('form_c_phone_country_prefix', phone_prefix))
            if fax_prefix:
                form_data.append(('form_c_fax_country_prefix', fax_prefix))

            # 确保 sess_id 正确
            form_data = [(k, v) if k != 'sess_id' else (k, self.sess_id) for k, v in form_data]

            logger.debug(f"Customer Data 表单字段数量: {len(form_data)}")
            logger.debug(f"Customer Data 表单字段 Keys: {[k for k, v in form_data]}")

            form_action = target_form.get('action', '')
            if form_action and not form_action.startswith('http'):
                submit_url = f"https://support.euserv.com/{form_action.lstrip('/')}"
            else:
                submit_url = form_action if form_action else url

            resp = self.session.post(submit_url, headers=headers, data=form_data)
            resp.raise_for_status()

            if SAVE_HTML_MODE:
                try:
                    with open('customer_data_confirm_response.html', 'w', encoding='utf-8') as f:
                        f.write(resp.text)
                    logger.debug("已保存 customer_data_confirm_response.html")
                except Exception as e:
                    logger.warning(f"保存响应 HTML 失败: {e}")

            # 服务器返回 JSON: {"rc": "100"} 表示成功
            try:
                result = json.loads(resp.text)
                rc = result.get('rc', '')
                rs = result.get('rs', '')
                if rc == '100':
                    logger.info(f"✅ Customer Data 确认成功: {rs}")
                    return True
                else:
                    logger.warning(f"⚠️ Customer Data 确认返回非成功状态: rc={rc}, rs={rs}")
                    errors = result.get('errors', {})
                    if errors:
                        logger.warning(f"   错误详情: {errors}")
                    return False
            except (json.JSONDecodeError, ValueError):
                still_has_warning = 'must be checked and confirmed' in resp.text
                if not still_has_warning:
                    logger.info("✅ Customer Data 确认成功")
                    return True
                else:
                    logger.warning("⚠️ Customer Data 确认后仍有警告，可能未完全成功")
                    return False

        except Exception as e:
            logger.error(f"⚠️ Customer Data 确认过程出错: {e}", exc_info=True)
            return False

    def get_servers(self) -> Dict[str, Tuple[bool, str]]:
        """获取账号下所有服务器及其续期状态"""
        logger.info(f"正在获取账号 {self.config.email} 的服务器列表...")

        if not self.sess_id:
            logger.error("❌ 未登录")
            return {}

        url = f"https://support.euserv.com/index.iphp?sess_id={self.sess_id}"
        headers = {'user-agent': USER_AGENT, 'origin': 'https://www.euserv.com'}

        try:
            detail_response = self.session.get(url=url, headers=headers)
            detail_response.raise_for_status()

            soup = BeautifulSoup(detail_response.text, 'html.parser')
            servers: Dict[str, Tuple[bool, str]] = {}

            selector = (
                '#kc2_order_customer_orders_tab_content_1 '
                '.kc2_order_table.kc2_content_table tr, '
                '#kc2_order_customer_orders_tab_content_2 '
                '.kc2_order_table.kc2_content_table tr'
            )
            for tr in soup.select(selector):
                server_id_cells = tr.select('.td-z1-sp1-kc')
                if len(server_id_cells) != 1:
                    continue

                row_text = tr.get_text().lower()
                server_id_text = server_id_cells[0].get_text().strip()
                logger.debug(f"合同 {server_id_text} 行内容: {row_text[:200]}...")

                if server_id_text in SKIP_CONTRACTS:
                    logger.info(f"⏭️ 跳过配置的合同: {server_id_text}")
                    continue

                # 跳过 Sync & Share 类型
                if 'sync' in row_text and 'share' in row_text:
                    logger.info(f"⏭️ 跳过 Sync & Share 合同: {server_id_text}")
                    continue

                action_containers = tr.select('.td-z1-sp2-kc .kc2_order_action_container')
                if not action_containers:
                    continue

                action_text = action_containers[0].get_text()
                can_renew = "Contract extension possible from" not in action_text
                can_renew_date = ""

                if not can_renew:
                    date_match = re.search(r'\b\d{4}-\d{2}-\d{2}\b', action_text)
                    if date_match:
                        can_renew_date = date_match.group(0)
                        can_renew = (
                            datetime.today().date()
                            >= datetime.strptime(can_renew_date, "%Y-%m-%d").date()
                        )

                servers[server_id_text] = (can_renew, can_renew_date)

            logger.info(f"✅ 账号 {self.config.email} 找到 {len(servers)} 台服务器")
            return servers

        except Exception as e:
            logger.error(f"❌ 获取服务器列表失败: {e}", exc_info=True)
            return {}

    def renew_server(self, order_id: str) -> bool:
        """执行服务器续期（6 步流程）"""
        logger.info(f"正在续期服务器 {order_id}...")

        url = "https://support.euserv.com/index.iphp"
        headers = {
            'user-agent': USER_AGENT,
            'Host': 'support.euserv.com',
            'origin': 'https://support.euserv.com',
            'Referer': 'https://support.euserv.com/index.iphp'
        }

        try:
            # 步骤 1：选择订单
            logger.info("步骤1: 选择订单...")
            resp1 = self.session.post(url, headers=headers, data={
                'Submit': 'Extend contract',
                'sess_id': self.sess_id,
                'ord_no': order_id,
                'subaction': 'choose_order',
                'show_contract_extension': '1',
                'choose_order_subaction': 'show_contract_details'
            })
            resp1.raise_for_status()
            logger.debug(f"[步骤1] 状态: {resp1.status_code}, 长度: {len(resp1.text)}")

            # 步骤 2：触发发送 PIN 邮件
            logger.info("步骤2: 触发发送 PIN 邮件...")
            pin_request_time = datetime.now()
            logger.debug(f"[步骤2] PIN 请求时间: {pin_request_time.strftime('%Y-%m-%d %H:%M:%S')}")
            resp2 = self.session.post(url, headers=headers, data={
                'sess_id': self.sess_id,
                'subaction': 'show_kc2_security_password_dialog',
                'prefix': 'kc2_customer_contract_details_extend_contract_',
                'type': '1'
            })
            resp2.raise_for_status()
            logger.debug(f"[步骤2] 状态: {resp2.status_code}, 摘要: {resp2.text[:200]}...")

            # 步骤 3：等待并获取续期 PIN
            logger.info("步骤3: 等待并获取续期 PIN 码...")
            time.sleep(5)
            pin = get_euserv_pin(
                self.config.email,
                self.config.email_password,
                self.config.imap_server,
                after_time=pin_request_time,
                pin_type='renew'
            )
            if not pin:
                logger.error("❌ 获取续期 PIN 码失败")
                return False

            # 步骤 4：验证 PIN，获取 token
            logger.info("步骤4: 验证 PIN 获取 token...")
            resp3 = self.session.post(url, headers=headers, data={
                'sess_id': self.sess_id,
                'auth': pin,
                'subaction': 'kc2_security_password_get_token',
                'prefix': 'kc2_customer_contract_details_extend_contract_',
                'type': '1',
                'ident': f'kc2_customer_contract_details_extend_contract_{order_id}'
            })
            resp3.raise_for_status()
            logger.debug(f"[步骤4] 响应: {resp3.text[:300]}...")

            result = json.loads(resp3.text)
            if result.get('rs') != 'success':
                logger.error(f"❌ 获取 token 失败: {result.get('rs', 'unknown')}")
                if 'error' in result:
                    logger.error(f"错误信息: {result['error']}")
                return False

            token = result['token']['value']
            logger.info("✅ 步骤4完成: 获取到 token")
            logger.debug(f"[步骤4] Token: {token[:30]}...")
            time.sleep(3)

            # 步骤 5：获取续期确认对话框
            logger.info("步骤5: 获取续期确认对话框...")
            resp4 = self.session.post(url, headers=headers, data={
                'sess_id': self.sess_id,
                'subaction': 'kc2_customer_contract_details_get_extend_contract_confirmation_dialog',
                'token': token
            })
            resp4.raise_for_status()
            logger.debug(f"[步骤5] 状态: {resp4.status_code}, 长度: {len(resp4.text)}")

            # 解析对话框 HTML
            dialog_html = ""
            try:
                result4 = json.loads(resp4.text)
                if isinstance(result4, dict):
                    if 'html' in result4 and 'value' in result4.get('html', {}):
                        dialog_html = result4['html']['value']
                    elif 'value' in result4:
                        dialog_html = result4['value']
            except Exception:
                dialog_html = resp4.text

            if SAVE_HTML_MODE:
                try:
                    with open('dialog_response.html', 'w', encoding='utf-8') as f:
                        f.write(dialog_html)
                    logger.debug("已保存 dialog_response.html")
                except Exception as e:
                    logger.warning(f"保存 dialog_response.html 失败: {e}")

            # 从对话框提取 subaction
            match_subaction = re.search(
                r'name=["\']subaction["\']\s+value=["\']([^"\']+)["\']', dialog_html
            )
            next_subaction = (
                match_subaction.group(1) if match_subaction
                else 'kc2_customer_contract_details_extend_contract_term'
            )
            if match_subaction:
                logger.info(f"🔍 从页面提取到 subaction: {next_subaction}")
            else:
                logger.warning(f"⚠️ 未能提取 subaction，使用默认值: {next_subaction}")

            # 步骤 6：提取所有 hidden input 并提交续期
            logger.debug(f"步骤6: 执行续期 ({next_subaction})...")
            data_confirm: Dict[str, str] = {}
            for input_match in re.finditer(
                r'<input[^>]+type=["\']hidden["\'][^>]*>', dialog_html, re.IGNORECASE
            ):
                input_tag = input_match.group(0)
                name_m = re.search(r'name=["\']([^"\']+)["\']', input_tag)
                value_m = re.search(r'value=["\']([^"\']+)["\']', input_tag)
                if name_m and value_m:
                    data_confirm[name_m.group(1)] = value_m.group(1)

            # 备用：若未提取到 token
            if 'token' not in data_confirm:
                logger.warning("⚠️ 未提取到 token，尝试备用正则...")
                token_m = re.search(
                    r'name=["\']token["\']\s+value=["\']([^"\']+)["\']', dialog_html
                )
                if token_m:
                    data_confirm['token'] = token_m.group(1)
                    logger.info(f"✅ 备用正则提取到 token: {data_confirm['token'][:20]}...")

            logger.info(f"📝 提交参数 Keys: {list(data_confirm.keys())}")

            time.sleep(2)
            resp5 = self.session.post(url, headers=headers, data=data_confirm)
            resp5.raise_for_status()

            if SAVE_HTML_MODE:
                try:
                    with open('final_response.html', 'w', encoding='utf-8') as f:
                        f.write(resp5.text)
                    logger.debug("已保存 final_response.html")
                except Exception as e:
                    logger.warning(f"保存 final_response.html 失败: {e}")

            html_lower = resp5.text.lower()
            if "error: token missing" in html_lower:
                logger.error("❌ 续期失败: 服务器返回 'Error: token missing'")
                return False

            success_keywords = [
                'successfully extended', 'erfolgreich', 'contract extended',
                'verlängert', 'extension successful', 'contract has been extended'
            ]
            for keyword in success_keywords:
                if keyword in html_lower:
                    logger.info(f"✅ 服务器 {order_id} 续期成功 (关键词: {keyword})")
                    return True

            # 步骤走完但无明确成功关键词时，假定成功（请自行查收邮件确认）
            logger.info(f"✅ 服务器 {order_id} 续期请求已提交（请查收邮件确认结果）")
            return True

        except Exception as e:
            logger.error(f"❌ 服务器 {order_id} 续期失败: {e}", exc_info=True)
            return False


# ============================================================
# Telegram 通知
# NOTE: 支持通过 TG_API_URL 环境变量设置自定义反代地址，解决国内无法直连问题
# ============================================================
TG_API_BASE = os.getenv("TG_API_URL", "https://api.telegram.org").rstrip('/')


def send_telegram(message: str, config: GlobalConfig) -> None:
    """发送 Telegram Bot 通知（支持自定义 API 地址）"""
    if not config.telegram_bot_token or not config.telegram_chat_id:
        logger.warning("⚠️ 未配置 Telegram，跳过通知")
        return

    url = f"{TG_API_BASE}/bot{config.telegram_bot_token}/sendMessage"
    data = {
        "chat_id": config.telegram_chat_id,
        "text": message,
        "parse_mode": "HTML"
    }

    try:
        response = requests.post(url, json=data, timeout=10)
        if response.status_code == 200:
            logger.info("✅ Telegram 通知发送成功")
        else:
            logger.error(f"❌ Telegram 通知失败: {response.status_code} - {response.text}")
    except Exception as e:
        logger.error(f"❌ Telegram 通知异常: {e}", exc_info=True)


# ============================================================
# 账号处理与主函数
# ============================================================
def process_account(account_config: AccountConfig, global_config: GlobalConfig) -> Dict:
    """处理单个账号的续期任务"""
    result = {
        'email': account_config.email,
        'success': False,
        'servers': {},
        'renew_results': [],
        'error': None
    }

    try:
        euserv = EUserv(account_config)

        # 登录（带重试）
        login_success = False
        for attempt in range(global_config.max_login_retries):
            if attempt > 0:
                logger.info(f"账号 {account_config.email} 第 {attempt + 1} 次登录尝试...")
                time.sleep(5)
            if euserv.login():
                login_success = True
                break

        if not login_success:
            result['error'] = "登录失败"
            return result

        # 确认 Customer Data 页面（解除面板功能限制）
        euserv.confirm_customer_data()

        # 获取服务器列表
        servers = euserv.get_servers()
        result['servers'] = servers

        if not servers:
            result['error'] = "未找到任何服务器"
            result['success'] = True
            return result

        # 检查并续期（只处理第一个可续期的合同）
        for order_id, (can_renew, can_renew_date) in servers.items():
            logger.info(f"检查服务器: {order_id}")
            if can_renew:
                logger.info(f"⏰ 服务器 {order_id} 可以续期，开始处理...")
                if euserv.renew_server(order_id):
                    result['renew_results'].append({
                        'order_id': order_id,
                        'success': True,
                        'message': f"✅ 服务器 {order_id} 续期成功"
                    })
                else:
                    result['renew_results'].append({
                        'order_id': order_id,
                        'success': False,
                        'message': f"❌ 服务器 {order_id} 续期失败"
                    })
                # 无论成功失败，每次只处理一个合同
                break
            else:
                logger.info(f"✓ 服务器 {order_id} 暂不需要续期（可续期日期: {can_renew_date}）")

        result['success'] = True

    except Exception as e:
        logger.error(f"处理账号 {account_config.email} 时发生异常: {e}", exc_info=True)
        result['error'] = str(e)

    return result


def main():
    """主函数"""
    logger.info("=" * 60)
    logger.info("EUserv 多账号自动续期脚本（多线程版本）")
    logger.info(f"执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"配置账号数: {len(ACCOUNTS)}")
    logger.info(f"最大并发线程: {GLOBAL_CONFIG.max_workers}")
    logger.info("=" * 60)

    if not ACCOUNTS:
        logger.error("❌ 未配置任何账号")
        sys.exit(1)

    all_results = []
    with ThreadPoolExecutor(max_workers=GLOBAL_CONFIG.max_workers) as executor:
        future_to_account = {
            executor.submit(process_account, account, GLOBAL_CONFIG): account
            for account in ACCOUNTS
        }

        for future in as_completed(future_to_account):
            account = future_to_account[future]
            try:
                result = future.result()
                all_results.append(result)
            except Exception as e:
                logger.error(f"处理账号 {account.email} 时发生未预期的异常: {e}", exc_info=True)
                all_results.append({
                    'email': account.email,
                    'success': False,
                    'error': f"未预期的异常: {str(e)}"
                })

    # 生成汇总报告
    logger.info("\n" + "=" * 60)
    logger.info("处理结果汇总")
    logger.info("=" * 60)

    message_parts = [f"<b>🔄 EUserv 多账号续期报告</b>\n"]
    message_parts.append(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    message_parts.append(f"处理账号数: {len(all_results)}\n")

    for result in all_results:
        email = result['email']
        logger.info(f"\n账号: {email}")
        message_parts.append(f"\n<b>📧 账号: {email}</b>")

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
            logger.info("  ✓ 所有服务器均无需续期")
            message_parts.append("  ✓ 所有服务器均无需续期")
            for order_id, (can_renew, can_renew_date) in servers.items():
                if can_renew_date:
                    message_parts.append(f"    订单 {order_id}: 可续期日期 {can_renew_date}")

    # 发送 Telegram 通知
    message = "\n".join(message_parts)
    send_telegram(message, GLOBAL_CONFIG)

    logger.info("\n" + "=" * 60)
    logger.info("执行完成")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
