# EUserv 自动续期脚本安装与使用指南

该脚本用于自动登录 EUserv 并在合同允许续期时执行续期操作。支持多账号、多线程，并通过 Telegram 发送运行报告。

## 1. 环境及系统依赖

由于脚本涉及验证码识别 (OCR)，需要安装系统级别的 Tesseract 引擎。

### 在 IPv6-only VPS (如 Debian/Ubuntu) 上安装：
首先建议配置 DNS64/NAT64 以确保可以访问 IPv4 网络：
```bash
echo -e "nameserver 2a00:1098:2b::1\nnameserver 2a01:4f8:c2c:123f::1\nnameserver 2001:4860:4860::8888" > /etc/resolv.conf
```

安装系统依赖：
```bash
apt-get update
apt-get install -y python3 python3-pip python3-venv tesseract-ocr tesseract-ocr-eng
```

### 在 Alpine (如青龙面板 Docker) 上安装：
```bash
apk add tesseract-ocr tesseract-ocr-data-eng
```

## 2. 安装 Python 依赖

建议在虚拟环境中运行：

```bash
# 创建并进入目录
mkdir -p /opt/euserv_renew && cd /opt/euserv_renew

# 创建虚拟环境
python3 -m venv venv
source venv/bin/activate

# 安装库
pip install Pillow requests beautifulsoup4 imap-tools pytesseract
```

## 3. 配置环境变量 (环境变量 / Config)

脚本通过环境变量读取配置。在青龙面板中，请在「环境变量」中添加：

### 核心配置 (必填)
| 变量名 | 说明 |
| :--- | :--- |
| `EUSERV_EMAIL` | EUserv 登录邮箱 |
| `EUSERV_PASSWORD` | EUserv 登录密码 |
| `EMAIL_PASS` | 邮箱第三方授权码（若与登录密码不同，**强烈建议设置**，用于自动提取 PIN 码） |

### 续期行为配置 (可选)
| 变量名 | 说明 |
| :--- | :--- |
| `SKIP_CONTRACTS` | **跳过的合同号**：多个用逗号隔开 (例: `12345,67890`) |
| `DEBUG` | 设置为 `true` 或 `html` 开启详细日志调试模式 |

### 通知推送配置 (可选)

脚本支持多种通知渠道，汇总运行结果：

#### 1. Telegram 推送
| 变量名 | 说明 |
| :--- | :--- |
| `TG_BOT_TOKEN` | Telegram 机器人 Token |
| `TG_CHAT_ID` | 接收消息的 Chat ID |
| `TG_API_URL` | 自定义 TG API 地址（国内 VPS 建议设置反代地址） |
| `TG_PROXY` | TG 专用代理地址 (例: `socks5://127.0.0.1:1080`) |

#### 2. 微信推送 (PushPlus)
| 变量名 | 说明 |
| :--- | :--- |
| `PUSH_PLUS_TOKEN` | [PushPlus](http://www.pushplus.plus/) 的 Token，可推送到微信公众号 |

#### 3. 自定义 WebHook 推送
| 变量名 | 说明 |
| :--- | :--- |
| `WECHAT_API_URL` | 自定义微信推送接口 URL |
| `WECHAT_AUTH_TOKEN` | 自定义接口的授权 Token |

## 4. 定时任务 (Crontab / 青龙)

### 青龙面板 (Qinglong):
1. **名称**: `EUserv 自动续期`
2. **命令**: `task euser_renew.py`
3. **定时规则**: `0 9 7,16 * *` (每月 7 号和 16 号的上午 9 点整运行)

### Crontab (常规 VPS):
```bash
30 8 * * * export EUSERV_EMAIL="..."; export EUSERV_PASSWORD="..."; /opt/euserv_renew/venv/bin/python /opt/euserv_renew/euser_renew.py >> /opt/euserv_renew/run.log 2>&1
```

## 5. 注意事项
1. **邮箱权限**：请确保登录邮箱开启了 **IMAP** 服务。
2. **两步验证 (PIN)**：脚本会自动读取 EUserv 发送的 PIN 邮件。
3. **识别率**：Tesseract OCR 并非 100% 成功，脚本内置了多次重试逻辑，通常重试几次即可通过验证码。
