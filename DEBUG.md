# Bot Debug Monitor

Real-time debug tool for inspecting bot internals: Telegram messages, Claude API calls, shell commands, and routing decisions.

## Quick Start

```bash
# Streaming mode (default, scrollable log)
python3 debug.py

# Full-screen interactive TUI
python3 debug.py --live

# Demo mode (no bot needed, pre-loaded sample data)
python3 debug.py --demo --live
```

The bot must be running for non-demo modes. The debug server starts automatically on `127.0.0.1:8766`.

## Modes

| Command | Description |
|---------|-------------|
| `python3 debug.py` | Streaming log output, scrollable terminal history |
| `python3 debug.py --live` | Full-screen interactive TUI with navigation |
| `python3 debug.py --raw` | Raw JSON Lines output (pipe to `jq`, etc.) |
| `python3 debug.py --demo --live` | TUI with pre-loaded sample events for testing |

## Common Options

| Flag | Description |
|------|-------------|
| `--filter api` | Only show `api_request` / `api_response` events |
| `--filter tg` | Only show `telegram_in` / `telegram_out` events |
| `--filter shell` | Only show `shell_exec` events |
| `--filter tool` | Only show `tool_call` events |
| `--filter route` | Only show `route` events |
| `--full` | Show complete data without truncation |
| `--host` / `--port` | Connect to a different address (default `127.0.0.1:8766`) |
| `--no-color` | Disable ANSI colors in streaming mode |

## Event Types

| Type | Source | Content |
|------|--------|---------|
| `telegram_in` | `router.py` | Full incoming Telegram update JSON |
| `telegram_out` | `telegram_client.py` | Outgoing API method + payload + status |
| `api_request` | `handlers.py` | Model, system prompt, messages, tools, round |
| `api_response` | `handlers.py` | Stop reason, text, usage tokens, tool calls |
| `shell_exec` | `handlers.py` | Command, output, exit code, handler type |
| `tool_call` | `handlers.py` | Tool name, input, result |
| `route` | `router.py` | Handler name, match reason, message text |

## Full-Screen TUI (`--live`)

### Three-Level Navigation

```
List View  ──Enter──>  Detail View  ──Enter──>  Value View
           <──Esc───               <──Esc───
```

**List View**: Event timeline with type, timestamp, and summary.

**Detail View**: JSON field browser with tree expansion. Nested objects/arrays can be expanded inline or drilled into as a new view.

**Value View**: Full text of a single field with scroll and search.

### Keyboard Shortcuts

All views:

| Key | Action |
|-----|--------|
| `j` / `↓` | Move down |
| `k` / `↑` | Move up |
| `Home` | Jump to top |
| `End` | Jump to bottom |
| `PgUp` | Page up |
| `PgDn` | Page down |
| `/` | Open search bar |
| `n` | Next search match |
| `N` | Previous search match |
| `Esc` | Clear search / go back / quit |

List view:

| Key | Action |
|-----|--------|
| `Enter` | Open selected event in detail view |
| `↓` past end | Re-enable auto-follow |

Detail view:

| Key | Action |
|-----|--------|
| `l` / `→` | Expand nested field (tree view) |
| `h` / `←` | Collapse field / jump to parent |
| `Enter` | Drill into nested field (new view) or view leaf value |
| `Backspace` | Go back |

Value view:

| Key | Action |
|-----|--------|
| `Enter` | Go back |

### Mouse Support

Works over SSH in Windows Terminal (SGR mouse protocol).

| Action | Effect |
|--------|--------|
| Click | Select row |
| Click selected row | Enter / drill in (same as Enter) |
| Scroll wheel | Scroll up / down |

### Search

Press `/` to open the search bar at the bottom of the current view. Type a keyword and press Enter. The search is case-insensitive and matches against:

- **List view**: event type + full JSON data
- **Detail view**: field keys + values
- **Value view**: line content (jumps to matching line)

Use `n` / `N` to jump between matches. Matching text is highlighted in yellow. Press `Esc` to clear the search.

### Auto-Follow

In list view, new events automatically scroll to the bottom (shown as `● auto`). Pressing `↑` or clicking disables auto-follow (`○ manual`). Pressing `↓` past the last event or `End` re-enables it.

## Architecture

```
debug_bus.py    Event bus singleton + TCP JSON Lines server
                - emit() is a no-op when no clients are connected
                - Runs as a daemon thread, started in bot.py

debug.py        CLI viewer + interactive TUI
                - Connects to debug_bus TCP server
                - Three output modes: streaming, live TUI, raw JSON
                - TUI uses Rich for rendering (emoji replaced with ◆)
```

### Instrumentation Points

Events are emitted via `debug_bus.emit(type, data)` at these locations:

| File | Events |
|------|--------|
| `router.py` | `telegram_in` (every update), `route` (dispatch decision) |
| `telegram_client.py` | `telegram_out` (sendMessage, editMessageText, sendPhoto, sendVideo) |
| `handlers.py` | `api_request`, `api_response`, `shell_exec`, `tool_call` |

### Performance

- Zero overhead when no debug client is connected (`if not _clients: return`)
- Event serialization runs in the calling thread (fast JSON encode)
- TCP server accepts up to 4 simultaneous clients
- Events are fire-and-forget; dead client sockets are cleaned up automatically

## Configuration

| Config Key | Default | Description |
|------------|---------|-------------|
| `debug_port` | `8766` | TCP port for the debug event server |

## Notes

- Emoji in `--live` mode are replaced with `◆` to avoid terminal width calculation mismatches between Rich and different terminal emulators (Windows Terminal, VS Code terminal, etc.)
- The debug server only listens on `127.0.0.1` (localhost), not exposed to the network
- Streaming mode (`python3 debug.py` without `--live`) displays emoji normally since it doesn't require precise width alignment
