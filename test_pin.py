#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
专门用于测试邮箱 PIN 码获取的工具
只连接邮箱，不登录 EUserv 官网，安全无风险
"""

import os
import re
import time
from imap_tools import MailBox

# 简单的日志输出
def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")

def get_imap_server(email: str) -> str:
    """自动判断 IMAP 服务器"""
    if "@qq.com" in email or "@foxmail.com" in email:
        return "imap.qq.com"
    elif "@163.com" in email:
        return "imap.163.com"
    elif "@outlook.com" in email or "@hotmail.com" in email:
        return "outlook.office365.com"
    return "imap.gmail.com"

def test_email():
    email = os.getenv("EUSERV_EMAIL", "")
    password = os.getenv("EMAIL_PASS", "")  # 注意：这里要是邮箱授权码
    
    if not email or not password:
        log("❌ 错误: 未设置 EUSERV_EMAIL 或 EMAIL_PASS 环境变量")
        return

    imap_server = get_imap_server(email)
    log(f"📧 邮箱: {email}")
    log(f"🖥️ 服务器: {imap_server}")
    log("🔑 正在尝试连接邮箱...")

    try:
        with MailBox(imap_server).login(email, password) as mailbox:
            log("✅ 邮箱登录成功！")
            log("🔍 正在搜索最近的 5 封邮件...")
            
            found_count = 0
            # 列出最近 5 封信，看看有没有来自 euserv 的
            for msg in mailbox.fetch(limit=5, reverse=True):
                is_euserv = 'euserv' in msg.from_ or 'euserv' in msg.subject.lower()
                prefix = "🎯 [EUserv]" if is_euserv else "   [其他]"
                
                log(f"{prefix} 时间: {msg.date_str} | 标题: {msg.subject}")
                
                if is_euserv:
                    found_count += 1
                    # 尝试匹配 PIN
                    match = re.search(r'PIN:\s*\n?(\d{6})', msg.text) or re.search(r'PIN.*(\d{6})', msg.text, re.DOTALL)
                    if match:
                        log(f"   ✅ -> 找到 PIN 码: {match.group(1)}")
                    else:
                        # 备用匹配
                        match_fallback = re.search(r'\b(\d{6})\b', msg.text)
                        if match_fallback:
                            log(f"   ⚠️ -> 未找到标准PIN格式，提取到疑似PIN: {match_fallback.group(1)}")
                        else:
                            log(f"   ❌ -> 未在邮件中提取到 6 位数字 PIN 码")
            
            if found_count == 0:
                log("⚠️ 未在最近 5 封邮件中找到 EUserv 的相关邮件")
            else:
                log(f"✨ 扫描完成，找到 {found_count} 封相关邮件")

    except Exception as e:
        log(f"❌ 连接或登录失败: {e}")
        log("提示: 如果是 Foxmail/QQ，请确保使用的是'授权码'而不是登录密码。")

if __name__ == "__main__":
    test_email()
