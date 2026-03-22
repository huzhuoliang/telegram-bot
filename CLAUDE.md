# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running

```bash
python3 bot.py
python3 bot.py --config /path/to/config.json
```

Send a notification from the CLI or other scripts:
```bash
python3 send.py "Backup completed"
# or via HTTP
curl -X POST http://127.0.0.1:8765/send -H 'Content-Type: application/json' -d '{"text":"hello"}'
```

## Systemd service

```bash
sudo cp telegram_bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram_bot
sudo journalctl -u telegram_bot -f
```

When `claude_backend = "api"`, also create `/etc/telegram_bot.env` (mode 600):
```
ANTHROPIC_API_KEY=sk-ant-...
```

## Architecture

```
bot.py              — entry point: threads, signal handling, startup/shutdown
telegram_client.py  — Telegram Bot API wrapper (30s long-poll, message chunking)
router.py           — chat_id auth gate + prefix dispatch
handlers.py         — ShellHandler, ClaudeHandler (cli/api), PresetHandler
notify_server.py    — localhost:8765 HTTP server for outbound notifications
send.py             — CLI helper to POST to notify server (stdlib only)
config.json         — presets, timeouts, model/backend settings
TOKEN.txt           — Telegram bot token (never commit)
CHAT_ID.txt         — authorized chat ID (never commit)
telegram_bot.service — systemd unit
```

**Threading:** main thread blocks on `_shutdown_event`; two daemon threads run the polling loop and the notify HTTP server. The notify server uses `server.timeout=1` + `handle_request()` loop (not `serve_forever()`) so shutdown is clean.

## Message routing (router.py)

Messages from any chat other than `CHAT_ID.txt` are silently dropped.

| Prefix / pattern | Handler |
|---|---|
| `!<cmd>` | ShellHandler — subprocess, stdout+stderr, exit code |
| `?<text>` | ClaudeHandler |
| `!clear` or `/clear` | Clears Claude conversation history |
| preset keyword | PresetHandler — dict lookup from config.json |
| anything else | ClaudeHandler (default fallback) |

## Claude backends (handlers.py)

Controlled by `claude_backend` in `config.json`:

- `"cli"` (default) — runs `claude -p <text>` subprocess. Uses existing Claude Code credentials. No API key needed. Stateless (no history).
- `"api"` — uses `anthropic` SDK directly. Requires `ANTHROPIC_API_KEY`. Maintains rolling conversation history (`claude_history_turns` turns). `anthropic` import is lazy (only loaded when this backend is active).

## config.json reference

| Key | Default | Description |
|-----|---------|-------------|
| `presets` | `{}` | Keyword → response mapping (case-insensitive) |
| `notify_port` | `8765` | Port for local HTTP notification server |
| `poll_interval` | `2` | Seconds to wait between retries on polling error |
| `shell_timeout` | `30` | Max seconds for shell command execution |
| `shell_output_max_chars` | `3000` | Truncation limit for shell output |
| `claude_backend` | `"cli"` | `"cli"` or `"api"` |
| `claude_model` | `"claude-sonnet-4-6"` | Model (used by `api` backend only) |
| `claude_max_tokens` | `1024` | Max tokens (used by `api` backend only) |
| `claude_history_turns` | `6` | Rolling history window (used by `api` backend only) |
| `claude_cli_timeout` | `60` | Subprocess timeout for `cli` backend |
| `log_file` | (none) | Optional log file path; stdout only if omitted |
| `log_level` | `"INFO"` | Logging level |
