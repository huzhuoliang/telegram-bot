**English** | [中文](README_zh.md)

# telegram-monitor

Personal Telegram bot service. Runs on your server, polls Telegram for messages, and lets any local script send notifications to your Telegram account.

## Features

- **Notifications** — other scripts/apps POST to a local HTTP endpoint to send messages, photos, or videos
- **Shell execution** — send `!<command>` to run it on the server and get output back
- **Claude AI** — send `?<question>` (or any text) to get an AI response; Claude can also search and send photos/videos inline
- **Preset replies** — configure fixed keyword → response pairs
- **Media archive** — forward photos/videos/documents to the bot and they are saved to the server automatically
- **Debug monitor** — real-time TUI to inspect Telegram I/O, Claude API calls, shell commands, and routing (see [DEBUG.md](DEBUG.md))
- **No public IP needed** — uses long-polling, no webhook required

## Prerequisites

- Python 3.10+
- `requests` and `anthropic` packages (`pip install requests anthropic`)
- A Telegram bot token (create one via [@BotFather](https://t.me/BotFather))
- Claude Code CLI installed and authenticated (for the default `cli` backend)

## Setup

**1. Save credentials**

```bash
echo "YOUR_BOT_TOKEN" > TOKEN.txt
echo "YOUR_CHAT_ID" > CHAT_ID.txt
```

To find your chat ID: start a conversation with your bot, then visit
`https://api.telegram.org/bot<TOKEN>/getUpdates` and look for `"chat":{"id":...}`.

**2. Run**

```bash
python3 bot.py
```

The bot sends "服务已启动。" to your Telegram when it's ready.

## Usage

Send messages to your bot in Telegram:

| Message | Action |
|---------|--------|
| `!ls -la /tmp` | Runs shell command, returns stdout + stderr + exit code |
| `?explain DNS` | Asks Claude, returns response in Chinese |
| `搜索一张XXX的照片` | Claude finds a photo and sends it to you |
| `ping` | Returns `pong` (preset) |
| `help` | Returns command reference (preset) |
| `!clear` or `/clear` | Clears Claude conversation history |
| any other text | Forwarded to Claude |

**Forwarding media:** Send or forward any photo, video, or document to the bot — it will be saved to `telegram_archive/` on the server and the bot will confirm with the saved path.

## Sending notifications from other scripts

While the bot is running, any local process can send messages, photos, or videos:

```bash
# Text
python3 send.py "Backup completed successfully"

# Photo (local file or URL)
python3 send.py --photo /tmp/screenshot.png --caption "今日报表"
python3 send.py --photo "https://example.com/chart.png"

# Video (local file or URL; up to 2 GB with local Bot API server)
python3 send.py --video /tmp/recording.mp4 --caption "录像"
python3 send.py --video "https://example.com/clip.mp4"
```

HTTP API (accepts local file path or URL for photo/video):

```bash
curl -X POST http://127.0.0.1:8765/send \
  -H 'Content-Type: application/json' \
  -d '{"text": "Deploy finished"}'

curl -X POST http://127.0.0.1:8765/send_photo \
  -H 'Content-Type: application/json' \
  -d '{"photo": "/tmp/img.jpg", "caption": "optional"}'

curl -X POST http://127.0.0.1:8765/send_video \
  -H 'Content-Type: application/json' \
  -d '{"video": "/tmp/clip.mp4", "caption": "optional"}'
```

## Configuration

Edit `config.json` to customize behavior:

```json
{
  "presets": {
    "ping": "pong",
    "status": "服务运行中。"
  },
  "proxy": "",
  "archive_dir": "telegram_archive",
  "notify_port": 8765,
  "shell_timeout": 30,
  "claude_backend": "cli",
  "claude_cli_timeout": 120,
  "telegram_api_base": "",
  "telegram_local_mode": false,
  "telegram_upload_limit_mb": 50
}
```

To use the local Bot API server (2 GB uploads), set:
```json
"telegram_api_base": "http://127.0.0.1:8081",
"telegram_local_mode": true,
"telegram_upload_limit_mb": 2000
```

### Claude backends

| `claude_backend` | Description |
|---|---|
| `"cli"` (default) | Uses `claude -p` CLI. Requires Claude Code to be installed and logged in. No API key needed. Stateless (no conversation history). |
| `"api"` | Uses Anthropic SDK directly. Requires `ANTHROPIC_API_KEY` env var. Supports rolling conversation history. |

Both backends support inline media delivery — Claude can respond with `[PHOTO: url]` markers that are automatically fetched and sent to you.

## Systemd services deployment

All services use `.service.example` templates. Copy and configure before installing.

### 1. Create environment files

```bash
# Project path (required by all services)
sudo mkdir -p /etc/telegram-bot
echo "PROJECT_DIR=$(pwd)" | sudo tee /etc/telegram-bot/project.env

# Telegram Bot API credentials (required only for local Bot API server)
sudo tee /etc/telegram-bot/api.env > /dev/null <<EOF
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
EOF
sudo chmod 600 /etc/telegram-bot/api.env

# Anthropic API key (required only for claude_backend="api")
# sudo tee /etc/telegram_bot.env > /dev/null <<EOF
# ANTHROPIC_API_KEY=sk-ant-...
# EOF
# sudo chmod 600 /etc/telegram_bot.env
```

### 2. Generate service files from templates

```bash
# Main bot service — replace YOUR_USER with your username
sed "s/YOUR_USER/$(whoami)/" telegram_bot.service.example > telegram_bot.service

# Docker services — no changes needed, just copy
cp telegram-bot-api.service.example telegram-bot-api.service
cp douyin-api.service.example douyin-api.service
```

### 3. Install and start

```bash
sudo cp telegram_bot.service telegram-bot-api.service douyin-api.service /etc/systemd/system/
sudo systemctl daemon-reload

# Main bot (required)
sudo systemctl enable --now telegram_bot

# Local Bot API server (optional, enables 2 GB uploads)
sudo systemctl enable --now telegram-bot-api

# Douyin downloader API (optional, for /dl douyin links)
sudo systemctl enable --now douyin-api
```

### Migrating to local Bot API server

The local Bot API server requires a one-time migration from the cloud API:

```bash
# 1. Stop bot
sudo systemctl stop telegram_bot

# 2. Log out from cloud API
curl "https://api.telegram.org/bot$(cat TOKEN.txt)/logOut"

# 3. Start local server and wait for it to be ready
sudo systemctl start telegram-bot-api

# 4. Verify
curl http://127.0.0.1:8081/bot$(cat TOKEN.txt)/getMe

# 5. Update config.json (set telegram_api_base, telegram_local_mode, telegram_upload_limit_mb)

# 6. Restart bot
sudo systemctl start telegram_bot
```

**Rollback:** Call `logOut` on the local server, wait 10 minutes (Telegram cooldown), clear `telegram_api_base` in config, restart bot.

### Daily operations

```bash
sudo systemctl status telegram_bot
sudo systemctl restart telegram_bot
sudo journalctl -u telegram_bot -f
```
