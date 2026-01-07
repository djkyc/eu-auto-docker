# EUserv 德鸡自动续期

EUserv 免费机需要每个月续期，本项目实现自动续期，支持 GitHub Actions 或 VPS/Docker 部署。

## 功能特点

- ✅ 自动登录并检测可续期的服务器
- ✅ 自动识别验证码（支持数学运算验证码）
- ✅ 自动获取邮箱 PIN 码验证
- ✅ 支持 Telegram 通知
- ✅ 支持跳过指定合同（如 Sync & Share）
- ✅ 支持 DEBUG 调试模式

---

## 快速开始

### 方式一：GitHub Actions 部署（推荐）

1. **Fork 本仓库**: 点击右上角的 "Fork" 按钮

2. **配置 Secrets**: 进入 `Settings` -> `Secrets and variables` -> `Actions`，添加以下变量：

| Secret 名称 | 必填 | 描述 |
|-------------|------|------|
| `EUSERV_EMAIL` | ✅ | EUserv 登录邮箱 |
| `EUSERV_PASSWORD` | ✅ | EUserv 登录密码 |
| `EMAIL_PASS` | ✅ | 邮箱应用专用密码（需开启 IMAP） |
| `TG_BOT_TOKEN` | ❌ | Telegram Bot Token（可选） |
| `TG_CHAT_ID` | ❌ | Telegram Chat ID（可选） |
| `SKIP_CONTRACTS` | ❌ | 要跳过的合同 ID，逗号分隔（如 `475282,123456`） |
| `DEBUG` | ❌ | 设为 `true` 开启详细调试日志 |

3. **等待运行**: 配置完成后，等待定时任务执行即可。也可以手动触发 workflow。

---

### 方式二：Docker 部署

1. **克隆仓库**
```bash
git clone https://github.com/你的用户名/euserv_py.git
cd euserv_py
```

2. **编辑配置文件**
```bash
nano docker-compose.yml
```

填入你的账号信息：
```yaml
environment:
  - EUSERV_EMAIL=你的邮箱
  - EUSERV_PASSWORD=你的密码
  - EMAIL_PASS=邮箱应用专用密码
  - TG_BOT_TOKEN=可选
  - TG_CHAT_ID=可选
  - SKIP_CONTRACTS=可选，跳过的合同ID
  - DEBUG=false
  - CRON_SCHEDULE=0 8 * * *
  - RUN_NOW=true
```

3. **启动容器**
```bash
docker compose up -d
```

4. **查看日志**
```bash
docker logs -f euserv-renew
```

---

## 环境变量说明

| 变量名 | 说明 | 示例 |
|--------|------|------|
| `EUSERV_EMAIL` | EUserv 登录邮箱 | `example@gmail.com` |
| `EUSERV_PASSWORD` | EUserv 登录密码 | `your_password` |
| `EMAIL_PASS` | 邮箱应用专用密码 | `abcd efgh ijkl mnop` |
| `TG_BOT_TOKEN` | Telegram Bot Token | `123456:ABC-DEF...` |
| `TG_CHAT_ID` | Telegram Chat ID | `123456789` |
| `SKIP_CONTRACTS` | 跳过的合同 ID（逗号分隔） | `475282,123456` |
| `DEBUG` | 开启调试模式 | `true` / `false` |
| `CRON_SCHEDULE` | Cron 定时表达式 | `0 8 * * *`（每天8点） |
| `RUN_NOW` | 启动时立即执行一次 | `true` / `false` |

---

## 邮箱配置说明

### Gmail
1. 开启两步验证
2. 生成应用专用密码：[Google 账号安全设置](https://myaccount.google.com/security)
3. 将 16 位密码填入 `EMAIL_PASS`

### QQ 邮箱 / Foxmail
1. 开启 IMAP 服务
2. 生成授权码
3. 将授权码填入 `EMAIL_PASS`

---

## 调试文件

脚本运行时会在容器内生成调试文件（每次覆盖）：

| 文件路径 | 说明 |
|----------|------|
| `/app/dialog_response.html` | 续期确认对话框 HTML |
| `/app/final_response.html` | 最终响应 HTML |

查看方法：
```bash
docker cp euserv-renew:/app/dialog_response.html ./
```

---

## 常见问题

**Q: 为什么会触发两次 PIN？**
A: 如果账号有多个合同（如 Sync & Share），可能会触发多次。使用 `SKIP_CONTRACTS` 跳过不需要的合同。

**Q: 连接超时怎么办？**
A: EUserv 服务器可能暂时不可用，稍后重试。或检查 VPS 网络。

**Q: 可以不用邮箱 PIN 吗？**
A: 可以！在 EUserv 后台关闭登录 PIN 验证，脚本会自动跳过邮件获取步骤。

---

## License

MIT
