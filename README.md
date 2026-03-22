# telegram-monitor

Personal Telegram bot service. Runs on your server, polls Telegram for messages, and lets any local script send notifications to your Telegram account.

## Features

- **Notifications** — other scripts/apps POST to a local HTTP endpoint to send messages
- **Shell execution** — send `!<command>` to run it on the server and get output back
- **Claude AI** — send `?<question>` (or any text) to get an AI response
- **Preset replies** — configure fixed keyword → response pairs
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

The bot sends "Bot started." to your Telegram when it's ready.

## Usage

Send messages to your bot in Telegram:

| Message | Action |
|---------|--------|
| `!ls -la /tmp` | Runs shell command, returns stdout + stderr + exit code |
| `?explain DNS` | Asks Claude, returns response |
| `ping` | Returns `pong` (preset) |
| `help` | Returns command reference (preset) |
| `!clear` | Clears Claude conversation history |
| any other text | Forwarded to Claude |

## Sending notifications from other scripts

While the bot is running, any local process can send a Telegram message:

```bash
# CLI helper
python3 send.py "Backup completed successfully"

# HTTP (any language)
curl -X POST http://127.0.0.1:8765/send \
  -H 'Content-Type: application/json' \
  -d '{"text": "Deploy finished"}'
```

## Configuration

Edit `config.json` to customize behavior:

```json
{
  "presets": {
    "ping": "pong",
    "status": "All systems operational."
  },
  "notify_port": 8765,
  "shell_timeout": 30,
  "claude_backend": "cli"
}
```

### Claude backends

| `claude_backend` | Description |
|---|---|
| `"cli"` (default) | Uses `claude -p` CLI. Requires Claude Code to be installed and logged in. No API key needed. |
| `"api"` | Uses Anthropic SDK directly. Requires `ANTHROPIC_API_KEY` env var. Supports conversation history. |

## Running as a systemd service

```bash
sudo cp telegram_bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram_bot
sudo journalctl -u telegram_bot -f
```

If using `claude_backend = "api"`, store the API key in `/etc/telegram_bot.env` (mode 600):
```
ANTHROPIC_API_KEY=sk-ant-...
```
