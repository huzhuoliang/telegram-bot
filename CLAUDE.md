# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running

```bash
python3 bot.py
python3 bot.py --config /path/to/config.json
```

Send notifications from the CLI or other scripts:
```bash
python3 send.py "Backup completed"
python3 send.py --photo /tmp/screenshot.png --caption "说明"
python3 send.py --video /tmp/recording.mp4 --caption "说明"

# HTTP endpoints
curl -X POST http://127.0.0.1:8765/send       -H 'Content-Type: application/json' -d '{"text":"hello"}'
curl -X POST http://127.0.0.1:8765/send       -H 'Content-Type: application/json' -d '{"text":"<b>bold</b>","parse_mode":"HTML"}'
curl -X POST http://127.0.0.1:8765/send_photo  -H 'Content-Type: application/json' -d '{"photo":"/tmp/img.jpg","caption":"optional"}'
curl -X POST http://127.0.0.1:8765/send_video  -H 'Content-Type: application/json' -d '{"video":"/tmp/clip.mp4","caption":"optional"}'
```

`/send` accepts an optional `parse_mode` field (`"HTML"` or `"MarkdownV2"`). Omit for plain text.

## Systemd service

**Service name: `telegram_bot`**

```bash
# Install (one-time)
sudo cp telegram_bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram_bot

# Daily operations
sudo systemctl status telegram_bot
sudo systemctl restart telegram_bot
sudo systemctl stop telegram_bot
sudo journalctl -u telegram_bot -f
```

When `claude_backend = "api"`, also create `/etc/telegram_bot.env` (mode 600):
```
ANTHROPIC_API_KEY=sk-ant-...
```

## Architecture

```
bot.py              — entry point: threads, signal handling, startup/shutdown
telegram_client.py  — Telegram Bot API wrapper (long-poll, send/delete/edit message,
                      send photo/video with URL→download fallback, download file)
router.py           — chat_id auth gate + message type dispatch
handlers.py         — ShellHandler, ClaudeHandler (cli/api), PrivilegedClaudeHandler,
                      PresetHandler, MediaArchiveHandler
notify_server.py    — localhost:8765 HTTP server for outbound notifications
send.py             — CLI helper to POST to notify server (stdlib only)
config.json         — presets, timeouts, model/backend settings
help.txt            — /help command text (static sections; hot-reloaded on each /help)
TOKEN.txt           — Telegram bot token (never commit)
CHAT_ID.txt         — authorized chat ID (never commit)
telegram_bot.service — systemd unit
```

**Threading:** main thread blocks on `_shutdown_event`; two daemon threads run the polling loop and the notify HTTP server. The notify server uses `server.timeout=1` + `handle_request()` loop (not `serve_forever()`) so shutdown is clean.

**getUpdates transport:** uses POST + JSON body (not GET + query params). Telegram does not reliably parse `allowed_updates` when sent as repeated GET params.

## Message routing (router.py)

Messages from any chat other than `CHAT_ID.txt` are silently dropped.

| Input | Handler |
|---|---|
| Photo + caption | ClaudeHandler — image recognition (api backend only) |
| Photo / video / document (no caption) | MediaArchiveHandler — saves to `archive_dir` |
| Emoji reaction on a message | Replies with the same emoji(s) |
| `!clear` or `/clear` | Clears Claude conversation history |
| `$clear` | Clears privileged Claude conversation history |
| `/ctx` | ClaudeHandler.context_stats() — context window breakdown (api only) |
| `$ctx` | PrivilegedClaudeHandler.context_stats() — privileged context breakdown (api only) |
| `$whitelist <list\|add\|remove>` | PrivilegedClaudeHandler.handle_whitelist_cmd() — manage shell whitelist |
| `!<cmd>` | ShellHandler — runs in `~`, sudo blocked |
| `$<text>` | PrivilegedClaudeHandler — runs in background thread; shell commands require user confirmation via reaction (👍 once / 📌 whitelist / 👎 reject); whitelisted commands skip confirmation |
| `?<text>` | ClaudeHandler |
| Preset keyword | PresetHandler — dict lookup from config.json |
| Anything else | ClaudeHandler (default fallback) |

Special commands are checked before prefix dispatch to avoid `!clear` being treated as a shell command.

## Claude backends (handlers.py)

Controlled by `claude_backend` in `config.json`:

- `"cli"` (default) — runs `claude -p <text>` subprocess. Uses existing Claude Code credentials. No API key needed. Stateless (no history).
- `"api"` — uses `anthropic` SDK directly. Requires `ANTHROPIC_API_KEY`. Maintains rolling conversation history (`claude_history_turns` turns). `anthropic` import is lazy (only loaded when this backend is active).

Both backends:
- Send `⏳ 处理中...` before calling the backend
- `api` backend: streams the response by editing the placeholder message in-place every 0.5 s (typewriter effect); cursor `▌` shown while generating. On completion the message is edited to the final HTML-formatted reply. When tool calls are needed, shows `🔧 执行工具中...` during execution then streams the follow-up response.
- `cli` backend: deletes the placeholder then sends a new message (no streaming).
- Parse `[PHOTO: url]` / `[VIDEO: url]` markers from Claude's response and send media automatically
- Always respond in Chinese (except code, shell output, technical strings)

### Claude action markers

Claude can embed these in its response to trigger media delivery:
```
[PHOTO: <url_or_path>]
[PHOTO: <url_or_path> | <caption>]
[VIDEO: <url_or_path>]
[VIDEO: <url_or_path> | <caption>]
```

For URLs that Telegram can't fetch directly (e.g. Wikipedia), `telegram_client` automatically downloads via local proxy and uploads as file.

## Media archive (handlers.py `MediaArchiveHandler`)

Incoming photos/videos/documents are saved to `archive_dir`:
```
~/telegram_archive/
├── photos/     YYYY-MM-DD_HH-MM-SS.jpg
├── videos/     YYYY-MM-DD_HH-MM-SS.<ext>
└── documents/  <original filename>
```

## config.json reference

| Key | Default | Description |
|-----|---------|-------------|
| `presets` | `{}` | Keyword → response mapping (case-insensitive) |
| `proxy` | `""` | HTTP proxy for Telegram API (e.g. `http://127.0.0.1:2080`) |
| `archive_dir` | `"~/telegram_archive"` | Directory for saved incoming media |
| `notify_port` | `8765` | Port for local HTTP notification server |
| `poll_interval` | `2` | Seconds to wait between retries on polling error |
| `shell_timeout` | `30` | Max seconds for shell command execution |
| `shell_output_max_chars` | `3000` | Truncation limit for shell output |
| `claude_backend` | `"cli"` | `"cli"` or `"api"` |
| `claude_model` | `"claude-sonnet-4-6"` | Model (used by `api` backend only) |
| `claude_max_tokens` | `1024` | Max tokens (used by `api` backend only) |
| `claude_history_turns` | `6` | Rolling history window (used by `api` backend only) |
| `claude_cli_timeout` | `120` | Subprocess timeout for `cli` backend |
| `privileged_claude_model` | `"claude-sonnet-4-6"` | Model for `$` privileged handler |
| `privileged_claude_max_tokens` | `4096` | Max tokens for privileged handler |
| `privileged_claude_history_turns` | `6` | Rolling history window for privileged handler |
| `privileged_claude_shell_timeout` | `60` | Shell command timeout for privileged handler |
| `privileged_shell_whitelist` | `[]` | Commands that skip confirmation; suffix `*` = prefix match, exact otherwise |
| `log_file` | (none) | Optional log file path; stdout only if omitted |
| `log_level` | `"INFO"` | Logging level |
