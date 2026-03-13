# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

EUserv auto-renewal script for free VPS servers. EUserv requires monthly renewal of free servers - this project automates the process via GitHub Actions or Docker/VPS deployment.

## Commands

### Run directly
```bash
# Set environment variables first
export EUSERV_EMAIL=your@email.com
export EUSERV_PASSWORD=your_password
export EMAIL_PASS=your_email_app_password  # IMAP app-specific password

# Run the renewal script
python euser_renew.py

# Test email PIN retrieval only (safe, doesn't login to EUserv)
python test_pin.py
```

### Docker deployment
```bash
# Build and run with docker-compose
docker-compose up -d

# View logs
docker logs euserv-renew
```

### Install dependencies
```bash
pip install -r requirements.txt
```

## Architecture

Single-file design with one main script:

- `euser_renew.py` - Core renewal logic with multi-account support and multi-threading
  - `AccountConfig` / `GlobalConfig` - Configuration classes
  - `EUserv` class - Handles login, server listing, and renewal workflow
  - Uses `ddddocr` for CAPTCHA recognition
  - Retrieves PIN codes from email via IMAP (`imap_tools`)
  - Optional Telegram notifications

- `test_pin.py` - Standalone email testing utility

## Key Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `EUSERV_EMAIL` | Yes | EUserv account email |
| `EUSERV_PASSWORD` | Yes | EUserv account password |
| `EMAIL_PASS` | Yes | Email IMAP app-specific password |
| `TG_BOT_TOKEN` | No | Telegram bot token for notifications |
| `TG_CHAT_ID` | No | Telegram chat ID |
| `SKIP_CONTRACTS` | No | Comma-separated contract IDs to skip |
| `DEBUG` | No | Debug mode: `true` for verbose logs, `html` for logs + save HTML files |
| `CRON_SCHEDULE` | No | Cron expression for Docker scheduled runs |
| `RUN_NOW` | No | Set to `true` to run immediately on Docker start |

## Renewal Flow

1. Login with CAPTCHA recognition (via ddddocr OCR)
2. Handle PIN verification (fetched from email via IMAP)
3. **Confirm Customer Data page** (auto-parse and submit the form to unblock panel functions)
4. Get server/contract list (filtering out Sync & Share contracts)
5. For each contract that can be renewed:
   - Trigger renewal PIN email
   - Fetch new PIN from email (distinguishes login vs renewal PINs by subject)
   - Complete multi-step renewal form submission with token
6. Send Telegram notification with results

## Email IMAP Support

Auto-detects IMAP server based on email domain:
- Gmail: `imap.gmail.com`
- QQ/Foxmail: `imap.qq.com`
- 163: `imap.163.com`
- Outlook/Hotmail: `outlook.office365.com`
