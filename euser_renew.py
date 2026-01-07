#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EUserv 自动续期脚本 - 多账号多线程版本
支持多账号配置、多线程并发处理、自动登录、验证码识别、检查到期状态、自动续期并发送 Telegram 通知
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

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(threadName)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

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


# ============== 配置区 ==============
# 全局配置
GLOBAL_CONFIG = GlobalConfig(
    telegram_bot_token=os.getenv("TG_BOT_TOKEN"),
    telegram_chat_id=os.getenv("TG_CHAT_ID"),
    max_workers=3,  # 建议不超过5，避免触发频率限制
    max_login_retries=3
)


# 自动判断 IMAP 服务器
def get_imap_server(email: str) -> str:
    """根据邮箱域名自动选择 IMAP 服务器"""
    if "@qq.com" in email or "@foxmail.com" in email:
        return "imap.qq.com"
    elif "@163.com" in email:
        return "imap.163.com"
    elif "@outlook.com" in email or "@hotmail.com" in email:
        return "outlook.office365.com"
    return "imap.gmail.com"  # 默认

# 账号列表配置
_email = os.getenv("EUSERV_EMAIL", "")
ACCOUNTS = [
    AccountConfig(
        email=_email,
        password=os.getenv("EUSERV_PASSWORD"),
        imap_server=get_imap_server(_email),
        email_password=os.getenv("EMAIL_PASS")  # 邮箱应用专用密码(QQ/Foxmail需使用授权码)
    ),
    # 添加更多账号示例：
    # AccountConfig(
    #     email="account2@gmail.com",
    #     password="password2",
    #     imap_server="imap.gmail.com",
    #     email_password="app_specific_password2"
    # ),
]

# ====================================


def recognize_and_calculate(captcha_image_url: str, session: requests.Session) -> Optional[str]:
    """识别并计算验证码（线程安全）"""
    logger.info("正在处理验证码...")
    try:
        logger.debug("尝试自动识别验证码...")
        response = session.get(captcha_image_url)
        # 直接使用原始图片数据进行识别，不做多余的预处理
        image_bytes = response.content
        
        # OCR 识别（加锁保证线程安全）
        with ocr_lock:
            text = ocr.classification(image_bytes).strip()
        
        logger.debug(f"OCR 识别文本: {text}")

        # 预处理：去除空格、大小写统一（右边字母转大写）
        raw_text = text.strip()
        text = raw_text.replace(' ', '').upper()  # 上面的正则要用大写匹配

        # 情况1：纯字母数字组合（没有运算符），直接返回原始识别文本（保留大小写）
        if re.fullmatch(r'[A-Z0-9]+', text):
            logger.info(f"检测到纯字母数字验证码: {raw_text}")
            return raw_text.strip()  # 保留原始大小写返回

        # 情况2：尝试解析四则运算
        # 支持的运算符：+ - * × x X / ÷
        pattern = r'^(\d+)([+\-*/×xX÷/])(\d+|[A-Z])$'
        match = re.match(pattern, text)

        if not match:
            logger.warning(f"无法解析验证码格式（非纯字母数字也非运算式）: {raw_text}")
            return raw_text.strip()  # 还是返回原始文本，交给上层处理或重试

        left_str, op, right_str = match.groups()
        left = int(left_str)

        # 处理右边：数字或字母（A=10 ... Z=35）
        if right_str.isdigit():
            right = int(right_str)
        else:  # 一定是单个大写字母（因为正则限制了）
            if 'A' <= right_str <= 'Z':
                right = ord(right_str) - ord('A') + 10
            else:
                logger.warning(f"右边字符无效: {right_str}")
                return raw_text.strip()

        # 根据运算符计算
        if op in {'*', '×', 'X', 'x'}:
            result = left * right
            op_name = '乘'
        elif op == '+':
            result = left + right
            op_name = '加'
        elif op == '-':
            result = left - right
            op_name = '减'
        elif op in {'/', '÷'}:
            if right == 0:
                logger.warning("除数为0，无法计算")
                return raw_text.strip()
            if left % right != 0:  # 如果不是整除，很多网站会拒绝非整数答案
                logger.warning(f"除法非整除: {left} ÷ {right} = {left / right}")
                return raw_text.strip()
            result = left // right
            op_name = '除'
        else:
            logger.warning(f"未知运算符: {op}")
            return raw_text.strip()

        logger.info(f"验证码计算: {left} {op_name} {right_str} = {result}")
        return str(result)
    except Exception as e:
        logger.error(f"验证码识别错误发生错误: {e}", exc_info=True)
        return None


def get_euserv_pin(email: str, email_password: str, imap_server: str, after_time: datetime = None, pin_type: str = 'login') -> Optional[str]:
    """从邮箱获取 EUserv PIN 码（带重试机制）
    
    Args:
        after_time: 只获取在此时间之后收到的邮件，用于区分新旧 PIN
        pin_type: PIN 类型，'login' 为登录 PIN，'renew' 为续期 PIN
    """
    max_retries = 12  # 最多尝试 12 次
    retry_interval = 5  # 每次间隔 5 秒
    # 总等待时间 = 60 秒
    
    # 根据类型定义匹配规则
    if pin_type == 'renew':
        # 续期 PIN 邮件标题包含 "Security Check" 或 "Confirmation"
        subject_keywords = ['security check', 'confirmation']
        type_name = "续期"
    else:
        # 登录 PIN 邮件标题包含 "Attempted Login" 或 "Login"
        subject_keywords = ['attempted login', 'login']
        type_name = "登录"
    
    logger.info(f"正在从邮箱 {email} 获取{type_name} PIN 码 (最长等待 {max_retries * retry_interval} 秒)...")
    if after_time:
        logger.debug(f"只查找 {after_time.strftime('%H:%M:%S')} 之后的邮件")
    
    for i in range(max_retries):
        try:
            if i > 0:
                logger.info(f"第 {i+1} 次尝试获取邮件...")
                time.sleep(retry_interval)
                
            with MailBox(imap_server).login(email, email_password) as mailbox:
                # 收集所有符合条件的邮件，然后选择最新的
                matched_emails = []
                
                for msg in mailbox.fetch(limit=10, reverse=True):
                    # 检查是否是 EUserv 的邮件
                    if 'euserv' not in msg.from_.lower():
                        continue
                    
                    subject_lower = msg.subject.lower()
                    
                    # 检查是否匹配目标类型的邮件
                    is_target_type = any(keyword in subject_lower for keyword in subject_keywords)
                    if not is_target_type:
                        continue
                    
                    # 如果指定了时间过滤，检查邮件时间（统一转为 UTC 时间戳比较）
                    email_timestamp = None
                    if msg.date:
                        email_dt = msg.date
                        if email_dt.tzinfo is None:
                            email_dt = email_dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
                        email_timestamp = email_dt.timestamp()
                        
                        if after_time:
                            filter_dt = after_time
                            if filter_dt.tzinfo is None:
                                filter_dt = filter_dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
                            
                            # 允许 2 分钟的误差（防止服务器时间偏差）
                            if email_timestamp < (filter_dt.timestamp() - 120):
                                logger.debug(f"跳过旧邮件: {msg.subject} (邮件时间: {email_dt})")
                                continue
                    
                    # 匹配正文中的 PIN
                    pin = None
                    match = re.search(r'PIN:\s*\n?(\d{6})', msg.text) or re.search(r'PIN.*?(\d{6})', msg.text, re.DOTALL)
                    if match:
                        pin = match.group(1)
                    else:
                        # 备用匹配：找正文里的 6 位数字
                        match_fallback = re.search(r'\b(\d{6})\b', msg.text)
                        if match_fallback:
                            pin = match_fallback.group(1)
                    
                    if pin:
                        matched_emails.append({
                            'pin': pin,
                            'timestamp': email_timestamp or 0,
                            'subject': msg.subject
                        })
                
                # 如果找到了匹配的邮件，返回时间最新的那个
                if matched_emails:
                    # 打印所有找到的邮件（调试用）
                    logger.info(f"📬 找到 {len(matched_emails)} 封匹配的{type_name}邮件:")
                    for idx, em in enumerate(matched_emails):
                        from datetime import datetime as dt
                        ts_str = dt.fromtimestamp(em['timestamp']).strftime('%H:%M:%S') if em['timestamp'] else 'N/A'
                        logger.info(f"  [{idx+1}] 时间: {ts_str}, PIN: {em['pin']}, 标题: {em['subject'][:40]}...")
                    
                    # 按时间戳降序排序，取最新的
                    matched_emails.sort(key=lambda x: x['timestamp'], reverse=True)
                    newest = matched_emails[0]
                    logger.info(f"✅ 选择最新的 PIN 码: {newest['pin']}")
                    return newest['pin']
                            
        except Exception as e:
            logger.warning(f"获取邮件尝试失败: {e}")
            
    logger.error(f"❌ 超时未找到{type_name} PIN 码邮件")
    return None


class EUserv:
    """EUserv 操作类"""
    
    def __init__(self, config: AccountConfig):
        self.config = config
        self.session = requests.Session()
        self.sess_id = None
        
    def login(self) -> bool:
        """登录 EUserv（支持验证码和 PIN）"""
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
            sess_id_match = re.search(r'sess_id["\']?\s*[:=]\s*["\']?([a-zA-Z0-9]{30,100})["\']?', sess.text)
            if not sess_id_match:
                sess_id_match = re.search(r'sess_id=([a-zA-Z0-9]{30,100})', sess.text)
            
            if not sess_id_match:
                logger.error("❌ 无法获取 sess_id")
                return False
            
            sess_id = sess_id_match.group(1)
            logger.debug(f"获取到 sess_id: {sess_id[:20]}...")
            
            # 访问 logo
            logo_png_url = "https://support.euserv.com/pic/logo_small.png"
            self.session.get(logo_png_url, headers=headers)
            
            # 提交登录表单
            login_data = {
                'email': self.config.email,
                'password': self.config.password,
                'form_selected_language': 'en',
                'Submit': 'Login',
                'subaction': 'login',
                'sess_id': sess_id
            }
            
            logger.debug("提交登录表单...")
            response = self.session.post(url, headers=headers, data=login_data)
            response.raise_for_status()

            # 检查登录错误
            if 'Please check email address/customer ID and password' in response.text:
                logger.error("❌ 用户名或密码错误")
                return False
            if 'kc2_login_iplock_cdown' in response.text:
                logger.error("❌ 密码错误次数过多，账号被锁定，请5分钟后重试")
                return False
            
            # 处理验证码
            if 'captcha' in response.text.lower():
                logger.info("⚠️ 需要验证码，正在识别...")
                captcha_code = recognize_and_calculate(captcha_url, self.session)
                
                if not captcha_code:
                    logger.error("❌ 验证码识别失败")
                    return False
                
                captcha_data = {
                    'subaction': 'login',
                    'sess_id': sess_id,
                    'captcha_code': captcha_code
                }
                
                response = self.session.post(url, headers=headers, data=captcha_data)
                response.raise_for_status()
                
                if 'captcha' in response.text.lower():
                    logger.error("❌ 验证码错误")
                    return False
            
            # 处理 PIN 验证
            if 'PIN that you receive via email' in response.text:
                logger.info("⚠️ 需要 PIN 验证")
                # 记录当前时间，用于过滤旧邮件
                pin_request_time = datetime.now()
                time.sleep(3)  # 等待邮件到达
                
                pin = get_euserv_pin(
                    self.config.email,
                    self.config.email_password,
                    self.config.imap_server,
                    after_time=pin_request_time,  # 只获取此时间之后的邮件
                    pin_type='login'
                )
                
                if not pin:
                    logger.error("❌ 获取 PIN 码失败")
                    return False
                
                soup = BeautifulSoup(response.text, "html.parser")
                login_confirm_data = {
                    'pin': pin,
                    'sess_id': sess_id,
                    'Submit': 'Confirm',
                    'subaction': 'login',
                    'c_id': soup.find("input", {"name": "c_id"})["value"],
                }
                response = self.session.post(url, headers=headers, data=login_confirm_data)
                response.raise_for_status()

            # 检查登录成功
            success_checks = [
                'Hello' in response.text,
                'Confirm or change your customer data here' in response.text,
                'logout' in response.text.lower() and 'customer' in response.text.lower()
            ]
            
            if any(success_checks):
                logger.info(f"✅ 账号 {self.config.email} 登录成功")
                self.sess_id = sess_id
                return True
            else:
                logger.error(f"❌ 账号 {self.config.email} 登录失败")
                return False
                
        except Exception as e:
            logger.error(f"❌ 登录过程出现异常: {e}", exc_info=True)
            return False
    
    def get_servers(self) -> Dict[str, Tuple[bool, str]]:
        """获取服务器列表"""
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
            servers = {}

            selector = '#kc2_order_customer_orders_tab_content_1 .kc2_order_table.kc2_content_table tr, #kc2_order_customer_orders_tab_content_2 .kc2_order_table.kc2_content_table tr'
            for tr in soup.select(selector):
                server_id = tr.select('.td-z1-sp1-kc')
                if len(server_id) != 1:
                    continue
                
                action_containers = tr.select('.td-z1-sp2-kc .kc2_order_action_container')
                if not action_containers:
                    continue
                    
                action_text = action_containers[0].get_text()
                server_id_text = server_id[0].get_text().strip()
                logger.debug(f"服务器 {server_id_text} 续期信息: {action_text[:100]}...")
                
                # 检查是否是失效/取消的合同，如果是则跳过
                skip_keywords = ['cancelled', 'expired', 'terminated', 'canceled', 'inactive']
                action_text_lower = action_text.lower()
                if any(keyword in action_text_lower for keyword in skip_keywords):
                    logger.info(f"⏭️ 跳过失效合同 {server_id_text}")
                    continue

                can_renew = action_text.find("Contract extension possible from") == -1
                can_renew_date = ""
                
                if not can_renew:
                    date_pattern = r'\b\d{4}-\d{2}-\d{2}\b'
                    match = re.search(date_pattern, action_text)
                    if match:
                        can_renew_date = match.group(0)
                        can_renew = datetime.today().date() >= datetime.strptime(can_renew_date, "%Y-%m-%d").date()

                servers[server_id_text] = (can_renew, can_renew_date)
            
            logger.info(f"✅ 账号 {self.config.email} 找到 {len(servers)} 台服务器")
            return servers
            
        except Exception as e:
            logger.error(f"❌ 获取服务器列表失败: {e}", exc_info=True)
            return {}
    
    def renew_server(self, order_id: str) -> bool:
        """续期服务器"""
        logger.info(f"正在续期服务器 {order_id}...")
        
        url = "https://support.euserv.com/index.iphp"
        headers = {
            'user-agent': USER_AGENT,
            'Host': 'support.euserv.com',
            'origin': 'https://support.euserv.com',
            'Referer': 'https://support.euserv.com/index.iphp'
        }
        
        try:
            # 步骤1: 选择订单
            logger.debug("步骤1: 选择订单...")
            data = {
                'Submit': 'Extend contract',
                'sess_id': self.sess_id,
                'ord_no': order_id,
                'subaction': 'choose_order',
                'show_contract_extension': '1',
                'choose_order_subaction': 'show_contract_details'
            }
            resp1 = self.session.post(url, headers=headers, data=data)
            resp1.raise_for_status()
            
            # 步骤2: 触发发送 PIN
            logger.debug("步骤2: 触发发送 PIN...")
            # 记录当前时间，用于过滤旧邮件
            pin_request_time = datetime.now()
            data = {
                'sess_id': self.sess_id,
                'subaction': 'show_kc2_security_password_dialog',
                'prefix': 'kc2_customer_contract_details_extend_contract_',
                'type': '1'
            }
            resp2 = self.session.post(url, headers=headers, data=data)
            resp2.raise_for_status()
            
            # 步骤3: 获取 PIN（只获取新邮件）
            logger.debug("步骤3: 等待并获取续期 PIN 码...")
            time.sleep(5)  # 等待邮件发送
            pin = get_euserv_pin(
                self.config.email,
                self.config.email_password,
                self.config.imap_server,
                after_time=pin_request_time,  # 只获取在触发之后收到的新邮件
                pin_type='renew'  # 续期类型的 PIN
            )
            
            if not pin:
                logger.error(f"❌ 获取续期 PIN 码失败")
                return False
        
            # 步骤4: 验证 PIN 获取 token
            logger.debug("步骤4: 验证 PIN 获取 token...")
            data = {
                'sess_id': self.sess_id,
                'auth': pin,
                'subaction': 'kc2_security_password_get_token',
                'prefix': 'kc2_customer_contract_details_extend_contract_',
                'type': '1',
                'ident': 'kc2_customer_contract_details_extend_contract_' + order_id
            }
            
            resp3 = self.session.post(url, headers=headers, data=data)
            resp3.raise_for_status()

            result = json.loads(resp3.text)
            if result.get('rs') != 'success':
                logger.error(f"❌ 获取 token 失败: {result.get('rs', 'unknown')}")
                if 'error' in result:
                    logger.error(f"错误信息: {result['error']}")
                return False
            
            token = result['token']['value']
            logger.debug(f"✅ 获取到 token: {token[:20]}...")
            time.sleep(3)

            # 步骤5: 获取续期确认对话框 (根据 JS 代码分析)
            # 之前的直接 extend_contract_term 可能已经失效，需要先获取确认页面
            logger.debug("步骤5: 获取续期确认对话框...")
            data = {
                'sess_id': self.sess_id,
                'subaction': 'kc2_customer_contract_details_get_extend_contract_confirmation_dialog',
                'token': token
            }
      
            resp4 = self.session.post(url, headers=headers, data=data)
            resp4.raise_for_status()
            
            # --- 解析确认对话框，寻找下一步动作 ---
            try:
                # 响应通常是 JSON: {"html": {"value": "...html content..."}}
                # 或者直接是 HTML
                dialog_html = ""
                try:
                    result4 = json.loads(resp4.text)
                    if isinstance(result4, dict):
                        if 'html' in result4 and 'value' in result4['html']:
                            dialog_html = result4['html']['value']
                        elif 'value' in result4:
                             dialog_html = result4['value']
                except:
                    dialog_html = resp4.text
                
                # 保存 HTML 以便分析（如果需要）
                with open('/app/dialog_response.html', 'w', encoding='utf-8') as f:
                    f.write(dialog_html)
                
                # 查找下一步的 subaction
                # 优先级1: 从 hidden input 中查找
                match_subaction = re.search(r'name=["\']subaction["\']\s+value=["\']([^"\']+)["\']', dialog_html)
                
                # 优先级2: 如果没找到，尝试默认值 (根据JS分析)
                next_subaction = match_subaction.group(1) if match_subaction else 'kc2_customer_contract_details_extend_contract_term'
                
                if match_subaction:
                    logger.info(f"🔍 从页面提取到 subaction: {next_subaction}")
                else:
                    logger.warning(f"⚠️ 未能从页面提取 subaction，尝试默认值: {next_subaction}")

                # 步骤6: 执行真正的续期
                # 关键：从对话框 HTML 表单中提取所有 hidden input 字段
                logger.debug(f"步骤6: 执行真正的续期 ({next_subaction})...")
                
                # 提取表单中所有的 hidden input 作为参数
                # 提取表单中所有的 hidden input 作为参数
                data_confirm = {}
                # 正则解析说明: finditer 匹配整个 input 标签, 然后分别匹配 name 和 value
                for match in re.finditer(r'<input[^>]+type=["\']hidden["\'][^>]*>', dialog_html, re.IGNORECASE):
                    input_tag = match.group(0)
                    name_match = re.search(r'name=["\']([^"\']+)["\']', input_tag)
                    value_match = re.search(r'value=["\']([^"\']+)["\']', input_tag)
                    if name_match and value_match:
                        field_name = name_match.group(1)
                        field_value = value_match.group(1)
                        data_confirm[field_name] = field_value

                # 备用：如果上面的正则因为属性顺序没匹配到 token
                if 'token' not in data_confirm:
                    logger.warning("⚠️ 标准正则未提取到 token，尝试备用正则...")
                    token_match = re.search(r'name=["\']token["\']\s+value=["\']([^"\']+)["\']', dialog_html)
                    if token_match:
                         data_confirm['token'] = token_match.group(1)
                         logger.info(f"✅ 备用正则提取到 token: {data_confirm['token'][:20]}...")

                # ⚠️ 关键修改：不要使用 self.sess_id 覆盖表单里的 sess_id
                # data_confirm['sess_id'] = self.sess_id 
                
                logger.info(f"📝 提交完整参数(Keys): {list(data_confirm.keys())}")
                if 'token' in data_confirm:
                    logger.debug(f"📝 Token: {data_confirm['token']}")

                # 确保 Referer 存在
                headers['Referer'] = 'https://support.euserv.com/index.iphp'
                
                time.sleep(2)
                resp5 = self.session.post(url, headers=headers, data=data_confirm)
                resp5.raise_for_status()
                
                # 保存最终响应
                with open('/app/final_response.html', 'w', encoding='utf-8') as f:
                    f.write(resp5.text)

                # 检查最终结果
                html_lower = resp5.text.lower()
                
                if "error: token missing" in html_lower:
                     logger.error("❌ 续期失败: 服务器返回 'Error: token missing' - 可能是参数提交格式错误或Referer缺失")
                     return False

                success_keywords = ['successfully extended', 'erfolgreich', 'contract extended', 'verlängert', 'extension successful', 'contract has been extended']
                for keyword in success_keywords:
                    if keyword in html_lower:
                        logger.info(f"✅ 服务器 {order_id} 续期成功 (找到关键词: {keyword})")
                        return True

                # 如果没有明确成功，但也无明确失败，且步骤走完了
                logger.info(f"✅ 服务器 {order_id} 续期请求已提交 (假设成功，请检查邮件)")
                return True

            except Exception as e:
                logger.error(f"❌ 解析确认对话框或提交续期失败: {e}", exc_info=True)
                return False
            logger.error(f"❌ JSON 解析失败: {e}", exc_info=True)
            return False
        except Exception as e:
            logger.error(f"❌ 服务器 {order_id} 续期失败: {e}", exc_info=True)
            return False


def send_telegram(message: str, config: GlobalConfig):
    """发送 Telegram 通知"""
    if not config.telegram_bot_token or not config.telegram_chat_id:
        logger.warning("⚠️ 未配置 Telegram，跳过通知")
        return
    
    url = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"
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
            logger.error(f"❌ Telegram 通知失败: {response.status_code}")
    except Exception as e:
        logger.error(f"❌ Telegram 异常: {e}", exc_info=True)


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
        
        # 登录（最多重试）
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
        
        # 获取服务器列表
        servers = euserv.get_servers()
        result['servers'] = servers
        
        if not servers:
            result['error'] = "未找到任何服务器"
            result['success'] = True  # 登录成功，只是没有服务器
            return result
        
        # 检查并续期（只续期第一个需要续期的合同）
        renewed = False
        for order_id, (can_renew, can_renew_date) in servers.items():
            logger.info(f"检查服务器: {order_id}")
            if can_renew:
                if renewed:
                    logger.info(f"⏭️ 跳过服务器 {order_id}（已有一个服务器续期成功）")
                    continue
                
                logger.info(f"⏰ 服务器 {order_id} 可以续期")
                if euserv.renew_server(order_id):
                    result['renew_results'].append({
                        'order_id': order_id,
                        'success': True,
                        'message': f"✅ 服务器 {order_id} 续期成功"
                    })
                    renewed = True  # 标记已续期，后续合同不再处理
                else:
                    result['renew_results'].append({
                        'order_id': order_id,
                        'success': False,
                        'message': f"❌ 服务器 {order_id} 续期失败"
                    })
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
    
    # 使用线程池处理多个账号
    all_results = []
    with ThreadPoolExecutor(max_workers=GLOBAL_CONFIG.max_workers) as executor:
        # 提交所有任务
        future_to_account = {
            executor.submit(process_account, account, GLOBAL_CONFIG): account 
            for account in ACCOUNTS
        }
        
        # 等待任务完成
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
