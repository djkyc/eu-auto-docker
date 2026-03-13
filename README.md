# euserv德鸡自动续期

euserv免费机需要每个月续期，本项目实现自动续期，支持github的action或者vps执行

## 1.主要功能
   
   实现每天自动登录，查找是否有需要可续期的机器，如果达到可以续期时间，则自动续期

## 2.action部署

 2.1  **Fork 本仓库**: 点击右上角的 "Fork" 按钮，将此项目复制到你自己的 GitHub 账户下。
 
 2.2  **配置 Secrets**: 在你 Fork 的仓库中，进入 `Settings` -> `Secrets and variables` -> `Actions`。点击 `New repository secret`，添加下面第3点的变量：
 
## 3.配置变量（github action部署，如果自己vps部署直接代码替换参数）

| Secret 名称       | 是否必须       | 描述                                                                                                                              |
| ----------------- | -------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `EUSERV_EMAIL`    | **是**   | 配置euserv登录邮箱，如果需要多账号续期配置多个AccountConfig对象 |
| `EUSERV_PASSWORD` | **是**   | 配置euserv登录密码，如果需要多账号续期配置多个AccountConfig对象 |
| `EMAIL_PASS`      | **是**   | 配置对应账号邮箱的应用专用密码（注意：这个密码需要去邮箱设置里面开启IMAP并生成应用专用密码，设置方法可以询问AI） |
| `TG_BOT_TOKEN`    | **否**   | 配置tg账号的token，非必须，不想收通知可以不配置                                              |
| `TG_CHAT_ID`      | **否**   | 配置tg账号的userid，非必须，不想收通知可以不配置                                                                        |
| `SKIP_CONTRACTS`  | **否**   | 要跳过的合同ID，逗号分隔（如 `475282,123456`） |
| `DEBUG`           | **否**   | 调试模式：`true` 开启详细日志，`html` 开启日志+保存HTML调试文件（仅Docker使用） |

## 4.运行

  以上配置完成后，等待定时执行就可以了，如果配置了tg信息运行后会收到通知

markdown
# 🚀 EUserv 自动续期脚本（多账号 + 多线程 + 验证码识别 + 微信/Telegram 通知）

[![Docker Pulls](https://img.shields.io/docker/pulls/ghcr.io/djkyc/euserv-tg-wx-diy)](https://github.com/your-repo)
[![License](https://img.shields.io/github/license/yourname/euserv-renew)](LICENSE)

本项目是一个基于 Python 的 EUserv 免费服务器自动续期脚本，支持**多账号并发处理**、**验证码自动识别**、**邮箱 PIN 自动提取**，并可将续期结果通过 **Telegram** 和 **微信** 推送到您的手机。脚本经过优化，稳定运行于 Docker 环境，适合部署在 NAS、VPS 或青龙面板等支持 Docker 的设备上。

---

## ✨ 特性

- ✅ 支持多个 EUserv 账号同时续期（多线程并发，默认最大 3 线程）
- ✅ 自动识别验证码（基于 `ddddocr`，支持算术验证码和字母数字组合，无需第三方打码平台）
- ✅ 自动从邮箱获取登录/续期 PIN 码（支持 Gmail、QQ、163、Outlook 等主流邮箱）
- ✅ 登录后自动确认 Customer Data 页面，解除面板功能限制
- ✅ 可跳过指定的合同 ID（如测试机、已废弃的机器）
- ✅ 支持 Telegram Bot 通知（HTML 格式）
- ✅ 支持微信推送（通过 API，自动移除 HTML 标签，纯文本展示）
- ✅ 完善的日志输出（支持 DEBUG 模式，可保存 HTML 用于排查）
- ✅ 打包为 Docker 镜像，即拉即用，也可自定义构建
- ✅ 支持 HTTP/HTTPS 代理，适应复杂网络环境

---

## 📦 快速开始

### 1. 准备工作

- 一个或多个 [EUserv](https://www.euserv.com) 账号
- 邮箱的 **IMAP 授权码**（用于接收 PIN 码）
  - Gmail：需开启两步验证并生成[应用专用密码](https://myaccount.google.com/apppasswords)
  - QQ/163/Outlook：使用邮箱的独立密码或授权码（在邮箱设置中获取）
- （可选）Telegram Bot Token 和 Chat ID
- （可选）微信推送 API 地址和 Token（可使用 [Server酱](https://sct.ftqq.com/) 或自建服务）

### 2. 准备环境变量文件

创建一个 `.env` 文件，填入您的配置。以下是一个完整示例：

```ini
# EUserv 账号配置（必填）
EUSERV_EMAIL=your_email@gmail.com
EUSERV_PASSWORD=your_password
EMAIL_PASS=your_imap_password_or_app_password

# 可选：跳过指定合同 ID（多个用逗号分隔）
SKIP_CONTRACTS=475282,123456

# 通知配置（Telegram，若不使用则留空）
TG_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TG_CHAT_ID=123456789

# 通知配置（微信，若不使用则留空）
WECHAT_API_URL=https://your-wechat-api.com/send
WECHAT_AUTH_TOKEN=your_token

# 调试模式（true/false/html，建议日常 false）
DEBUG=false

# 代理（可选）
HTTP_PROXY=http://192.168.1.100:7890
HTTPS_PROXY=http://192.168.1.100:7890
环境变量详细说明：

变量	必填	说明
EUSERV_EMAIL	✅	EUserv 登录邮箱
EUSERV_PASSWORD	✅	EUserv 登录密码
EMAIL_PASS	✅	邮箱 IMAP 密码/授权码（用于接收 PIN，具体见上文）
SKIP_CONTRACTS	❌	要跳过的合同 ID，多个用逗号分隔
TG_BOT_TOKEN	❌	Telegram Bot Token（从 @BotFather 获取）
TG_CHAT_ID	❌	Telegram 接收人的 Chat ID（可通过 @userinfobot 获取）
WECHAT_API_URL	❌	微信推送 API 地址（需支持 POST 表单）
WECHAT_AUTH_TOKEN	❌	微信推送认证令牌（将放在请求体 token 字段）
DEBUG	❌	true 输出 DEBUG 日志；html 额外保存 HTML 文件到容器内
HTTP_PROXY/HTTPS_PROXY	❌	代理地址（如果网络需要）
注意：EMAIL_PASS 视邮箱服务商而定：

Gmail：应用专用密码（16位）

QQ/163：授权码（登录邮箱设置中获取）

Outlook：普通密码或应用密码均可

🐳 使用 Docker 运行
方式一：使用预构建镜像（推荐）
镜像已包含所有依赖，直接拉取运行：

bash
docker run --rm \
  --env-file /path/to/your/.env \
  ghcr.io/djkyc/euserv-tg-wx-diy:latest
方式二：挂载本地修改后的脚本（用于调试或自定义）
如果您修改了脚本（例如调整推送格式），可以通过挂载覆盖容器内的文件：

bash
docker run --rm \
  --env-file /path/to/your/.env \
  -v /path/to/your/euser_renew.py:/app/euser_renew.py \
  ghcr.io/djkyc/euserv-tg-wx-diy:latest
方式三：青龙面板定时任务
在青龙面板中创建定时任务，命令填写：

bash
docker run --rm --env-file /mnt/usb/euserv/.env ghcr.io/djkyc/euserv-tg-wx-diy:latest
建议将 .env 文件放在容器可访问的目录，并设置合理的定时（例如每月 1 号执行）。

🔧 自定义构建镜像
如果您想基于修改后的脚本构建自己的镜像，可参考以下步骤：

1. 准备文件
将修改后的 euser_renew.py 和下面的 Dockerfile 放在同一目录。

Dockerfile 内容：

dockerfile
FROM ghcr.io/djkyc/euserv-tg-wx-diy:latest
COPY euser_renew.py /app/euser_renew.py
2. 构建镜像
bash
docker build -t your-name/your-image:latest .
3. 运行自定义镜像
bash
docker run --rm --env-file /path/to/.env your-name/your-image:latest
📝 通知效果展示
Telegram	微信
https://via.placeholder.com/400x300?text=Telegram+Screenshot	https://via.placeholder.com/400x300?text=WeChat+Screenshot
微信推送会自动移除 HTML 标签，仅显示纯文本内容，避免出现 <b> 等标记，保证排版清晰。

使用方式✅ 快速测试命令（使用挂载）
将上述脚本保存为 /mnt/usb/euserv/euser_renew.py，然后使用挂载方式运行：

bash
docker run --rm \
  --env-file /mnt/usb/euserv/.env \
  -v /mnt/usb/euserv/euser_renew.py:/app/euser_renew.py \
  ghcr.io/djkyc/euserv-tg-wx-diy:latest
测试无误后，可按需构建自定义镜像。






⚙️ 脚本工作原理
登录：模拟浏览器登录 EUserv，遇到验证码自动识别（支持算术验证码和字母数字组合）。

PIN 验证：若需要 PIN，自动连接 IMAP 服务器提取最新邮件中的 6 位 PIN 码。

确认客户资料：若登录后要求确认 Customer Data，自动填写并提交表单。

获取服务器列表：解析合同页面，识别可续期的服务器（跳过配置的合同和 Sync & Share 类型）。

续期：对第一个可续期的合同执行续期流程（触发 PIN → 获取 token → 提交续期）。

通知：将结果汇总后发送至 Telegram 和/或微信。

❗ 注意事项
请确保邮箱 IMAP 服务已开启，并且密码/授权码正确。

如果使用 Gmail，必须开启两步验证并生成应用专用密码。

微信推送接口需支持 POST 表单，参数为 title、content、token。您可以使用 Server酱 或自建服务。

为避免触发 EUserv 频率限制，默认最大并发线程数为 3，您可在脚本中调整 max_workers 变量。

若出现“跳过配置的合同”提示，请检查 SKIP_CONTRACTS 环境变量是否正确设置。

脚本默认只处理 第一个可续期的合同（无论成功失败），这是为了避免单个账号续期多个合同导致的复杂情况，如需调整可修改 process_account 中的循环逻辑。

🤝 贡献
欢迎提交 Issue 或 Pull Request 来改进脚本。如果您觉得本项目有帮助，请给个 ⭐ 鼓励一下！

📄 许可证
MIT License © 2025 [你的名字]

提示：本脚本仅供学习交流使用，请勿滥用。使用前请确保您已阅读并同意 EUserv 的服务条款。
text

                 

