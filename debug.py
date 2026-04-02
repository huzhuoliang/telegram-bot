#!/usr/bin/env python3
"""Bot debug monitor — real-time CLI viewer for bot events.

Usage:
    python3 debug.py                  # streaming mode (default, scrollable)
    python3 debug.py --live           # full-screen TUI mode
    python3 debug.py --filter api     # only show api_request / api_response
    python3 debug.py --filter shell   # only show shell_exec
    python3 debug.py --filter tg      # only show telegram_in / telegram_out
    python3 debug.py --raw            # output raw JSON Lines (pipe-friendly)
    python3 debug.py --full           # show complete data (not truncated)
"""

import argparse
import json
import socket
import sys
import unicodedata
from datetime import datetime

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

# ── Colour / label mapping ──────────────────────────────────────────────────

# Labels: padded to uniform width in _label() below
TYPE_STYLES = {
    "telegram_in":   ("bold cyan",    "TG IN"),
    "telegram_out":  ("bold blue",    "TG OUT"),
    "api_request":   ("bold yellow",  "API >>"),
    "api_response":  ("bold green",   "API <<"),
    "shell_exec":    ("bold magenta", "SHELL"),
    "tool_call":     ("bold red",     "TOOL"),
    "route":         ("dim",          "ROUTE"),
}

_LABEL_WIDTH = max(len(v[1]) for v in TYPE_STYLES.values())


def _label(etype: str) -> str:
    _, raw = TYPE_STYLES.get(etype, ("", etype[:6]))
    return raw.ljust(_LABEL_WIDTH)

# ANSI colors for plain/streaming mode
_ANSI = {
    "telegram_in":   "\033[1;36m",   # bold cyan
    "telegram_out":  "\033[1;34m",   # bold blue
    "api_request":   "\033[1;33m",   # bold yellow
    "api_response":  "\033[1;32m",   # bold green
    "shell_exec":    "\033[1;35m",   # bold magenta
    "tool_call":     "\033[1;31m",   # bold red
    "route":         "\033[2m",      # dim
}
_RESET = "\033[0m"
_DIM = "\033[2m"


def _is_emoji_codepoint(cp: int) -> bool:
    """Return True if the codepoint is an emoji that terminals render as 2 cells."""
    return (
        0x1F300 <= cp <= 0x1FAFF    # Misc Symbols & Pictographs, Emoticons, etc.
        or 0x2600 <= cp <= 0x27BF   # Misc Symbols, Dingbats
        or 0x2300 <= cp <= 0x23FF   # Misc Technical (⌚⏳ etc.)
        or 0x2B05 <= cp <= 0x2B55   # Arrows, geometric shapes
        or 0x2934 <= cp <= 0x2935
        or 0x25AA <= cp <= 0x25FE
        or 0x2700 <= cp <= 0x27BF
        or 0x3030 == cp or 0x303D == cp
        or 0x3297 == cp or 0x3299 == cp
        or 0x1F000 <= cp <= 0x1F02F  # Mahjong, Domino
        or 0x1F0A0 <= cp <= 0x1F0FF  # Playing cards
        or 0x1F100 <= cp <= 0x1F1FF  # Enclosed Alphanumerics, Regional Indicators
        or 0x1F200 <= cp <= 0x1F2FF  # Enclosed Ideographic
    )


def _split_graphemes(s: str) -> list[str]:
    """Split string into grapheme clusters for width calculation.

    Groups these multi-codepoint sequences into one cluster:
    - Regional Indicator pairs (flags): 🇺🇸 = U+1F1FA U+1F1F8
    - Emoji + skin tone modifier: 👍🏻 = U+1F44D U+1F3FB
    - ZWJ sequences: 👨‍💻 = U+1F468 U+200D U+1F4BB
    - Keycap sequences: 1️⃣ = U+0031 U+FE0F U+20E3
    - Emoji + VS16: 🖥️ = U+1F5A5 U+FE0F
    """
    clusters = []
    i = 0
    n = len(s)
    while i < n:
        cp = ord(s[i])

        # Regional indicator pair (flags)
        if 0x1F1E6 <= cp <= 0x1F1FF and i + 1 < n and 0x1F1E6 <= ord(s[i + 1]) <= 0x1F1FF:
            clusters.append(s[i:i + 2])
            i += 2
            continue

        # Start of a potential emoji sequence — consume modifiers/ZWJ
        if _is_emoji_codepoint(cp) or (cp < 0x80 and i + 1 < n and ord(s[i + 1]) == 0xFE0F):
            # Keycap: digit/# /* + FE0F + 20E3
            if cp < 0x80 and i + 1 < n and ord(s[i + 1]) == 0xFE0F:
                end = i + 2
                if end < n and ord(s[end]) == 0x20E3:
                    end += 1
                clusters.append(s[i:end])
                i = end
                continue

            j = i + 1
            # Consume VS16
            if j < n and ord(s[j]) == 0xFE0F:
                j += 1
            # Consume skin tone modifier (U+1F3FB..U+1F3FF)
            if j < n and 0x1F3FB <= ord(s[j]) <= 0x1F3FF:
                j += 1
            # Consume ZWJ chains: (ZWJ + emoji [+ VS16] [+ skin tone])*
            while j < n and ord(s[j]) == 0x200D:
                j += 1  # skip ZWJ
                if j >= n:
                    break
                next_cp = ord(s[j])
                if _is_emoji_codepoint(next_cp):
                    j += 1
                    if j < n and ord(s[j]) == 0xFE0F:
                        j += 1
                    if j < n and 0x1F3FB <= ord(s[j]) <= 0x1F3FF:
                        j += 1
                else:
                    break
            # Consume trailing combining/tag chars
            while j < n:
                nc = ord(s[j])
                if nc == 0xFE0F or nc == 0xFE0E or unicodedata.category(s[j]).startswith("M"):
                    j += 1
                elif 0xE0020 <= nc <= 0xE007F:  # tag characters
                    j += 1
                else:
                    break
            clusters.append(s[i:j])
            i = j
            continue

        # Regular character (possibly with combining marks)
        j = i + 1
        while j < n and unicodedata.category(s[j]).startswith("M"):
            j += 1
        clusters.append(s[i:j])
        i = j

    return clusters


def _cluster_width(cluster: str) -> int:
    """Width of a grapheme cluster in terminal cells."""
    if len(cluster) == 0:
        return 0
    cp0 = ord(cluster[0])

    # Multi-codepoint emoji sequences → always 2 cells
    if len(cluster) > 1:
        # Regional indicator pair (flag)
        if 0x1F1E6 <= cp0 <= 0x1F1FF:
            return 2
        # Keycap sequence (digit + FE0F + 20E3)
        if cp0 < 0x80 and any(ord(c) == 0x20E3 for c in cluster):
            return 2
        # Emoji + VS16, skin tone, ZWJ sequence
        if _is_emoji_codepoint(cp0):
            return 2
        # Base char + VS16 only (text char promoted to emoji presentation)
        if len(cluster) >= 2 and ord(cluster[1]) == 0xFE0F:
            return 2

    # Single emoji codepoint
    if _is_emoji_codepoint(cp0):
        return 2

    # Zero-width chars that somehow ended up alone
    if cp0 in (0x200D, 0xFE0E, 0xFE0F) or unicodedata.category(cluster[0]).startswith("M"):
        return 0

    # CJK / fullwidth
    eaw = unicodedata.east_asian_width(cluster[0])
    if eaw in ("W", "F"):
        return 2
    return 1


def _strip_emoji(s: str) -> str:
    """Replace all emoji grapheme clusters with '*' for terminal compatibility."""
    result = []
    for g in _split_graphemes(s):
        if len(g) == 0:
            continue
        cp0 = ord(g[0])
        is_emoji = (
            _is_emoji_codepoint(cp0)
            or (0x1F1E6 <= cp0 <= 0x1F1FF)  # regional indicator
            or (len(g) > 1 and ord(g[1]) == 0xFE0F)  # text + VS16
            or (cp0 < 0x80 and any(ord(c) == 0x20E3 for c in g))  # keycap
        )
        result.append("◆" if is_emoji else g)
    return "".join(result)


# ── String width / truncation (grapheme-aware) ──────────────────────────────

def _display_width(s: str) -> int:
    """Terminal display width of a string."""
    return sum(_cluster_width(g) for g in _split_graphemes(s))


def _truncate_to_width(s: str, max_width: int) -> str:
    """Truncate string to fit within max_width terminal columns.
    Never splits a grapheme cluster."""
    w = 0
    result = []
    for g in _split_graphemes(s):
        gw = _cluster_width(g)
        if gw > 0 and w + gw > max_width - 1:
            result.append("…")
            return "".join(result)
        w += gw
        result.append(g)
    return s  # no truncation needed


def _ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%H:%M:%S.%f")[:-3]


# ── Formatters ───────────────────────────────────────────────────────────────

def _truncate(s: str, maxlen: int = 200) -> str:
    if len(s) <= maxlen:
        return s
    return s[:maxlen] + f"…({len(s)} chars)"


def _format_telegram_in(data: dict) -> str:
    msg = data.get("message") or data.get("edited_message") or {}
    text = msg.get("text", "")
    caption = msg.get("caption", "")
    user = msg.get("from", {}).get("first_name", "?")
    media = ""
    if "photo" in msg:
        media = " [photo]"
    elif "video" in msg:
        media = " [video]"
    elif "document" in msg:
        media = f" [doc: {msg['document'].get('file_name', '?')}]"
    content = text or caption or "(no text)"
    return f"{user}: {_truncate(content)}{media}"


def _format_telegram_out(data: dict) -> str:
    method = data.get("method", "?")
    payload = data.get("payload", {})
    text = payload.get("text", "")
    caption = payload.get("caption", "")
    content = text or caption
    status = data.get("status", "")
    status_str = f" [{status}]" if status else ""
    if content:
        return f"{method}{status_str}: {_truncate(content)}"
    return f"{method}{status_str}"


def _format_api_request(data: dict) -> str:
    model = data.get("model", "?")
    n_msgs = len(data.get("messages", []))
    system_len = len(data.get("system", ""))
    tools = data.get("tools", [])
    return f"model={model}  msgs={n_msgs}  system={system_len}ch  tools={len(tools)}"


def _format_api_response(data: dict) -> str:
    stop = data.get("stop_reason", "?")
    usage = data.get("usage", {})
    inp = usage.get("input_tokens", 0)
    out = usage.get("output_tokens", 0)
    text = _truncate(data.get("text", ""), 120)
    tool_calls = data.get("tool_calls", [])
    tc_str = f"  tool_calls={len(tool_calls)}" if tool_calls else ""
    return f"stop={stop}  in={inp} out={out}{tc_str}  {text}"


def _format_shell_exec(data: dict) -> str:
    cmd = data.get("command", "?")
    output = _truncate(data.get("output", ""), 120).replace("\n", " ")
    return f"$ {cmd}\n→ {output}"


def _format_tool_call(data: dict) -> str:
    name = data.get("name", "?")
    inp = json.dumps(data.get("input", {}), ensure_ascii=False)
    result = _truncate(data.get("result", ""), 120).replace("\n", " ")
    return f"{name}({_truncate(inp, 100)})\n→ {result}"


def _format_route(data: dict) -> str:
    handler = data.get("handler", "?")
    reason = data.get("reason", "")
    return f"→ {handler}" + (f" ({reason})" if reason else "")


_FORMATTERS = {
    "telegram_in":  _format_telegram_in,
    "telegram_out": _format_telegram_out,
    "api_request":  _format_api_request,
    "api_response": _format_api_response,
    "shell_exec":   _format_shell_exec,
    "tool_call":    _format_tool_call,
    "route":        _format_route,
}


def _format_full(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


# ── Filter matching ──────────────────────────────────────────────────────────

_FILTER_MAP = {
    "api":   {"api_request", "api_response"},
    "tg":    {"telegram_in", "telegram_out"},
    "shell": {"shell_exec"},
    "tool":  {"tool_call"},
    "route": {"route"},
}


def _matches_filter(event_type: str, filter_key: str | None) -> bool:
    if not filter_key:
        return True
    allowed = _FILTER_MAP.get(filter_key)
    if allowed:
        return event_type in allowed
    return filter_key in event_type


# ── Connection ───────────────────────────────────────────────────────────────

def connect(host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((host, port))
    return sock


def iter_events(sock: socket.socket):
    """Yield parsed events from the socket."""
    buf = b""
    while True:
        try:
            chunk = sock.recv(4096)
        except (ConnectionResetError, OSError):
            break
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


# ── Raw mode ─────────────────────────────────────────────────────────────────

def run_raw(sock: socket.socket, filter_key: str | None):
    for event in iter_events(sock):
        if _matches_filter(event.get("type", ""), filter_key):
            print(json.dumps(event, ensure_ascii=False), flush=True)


# ── Streaming mode (default) ────────────────────────────────────────────────

def run_stream(sock: socket.socket, filter_key: str | None, full: bool = False,
               use_color: bool = True):
    """Print each event as it arrives — scrollable terminal history."""
    # indent width = len("HH:MM:SS.mmm") + 2 + _LABEL_WIDTH + 2
    indent = 12 + 2 + _LABEL_WIDTH + 2

    for event in iter_events(sock):
        etype = event.get("type", "")
        if not _matches_filter(etype, filter_key):
            continue

        ts = _ts(event.get("ts", 0))
        label = _label(etype)

        if full:
            details = _format_full(event.get("data", {}))
        else:
            formatter = _FORMATTERS.get(etype)
            if formatter:
                details = formatter(event.get("data", {}))
            else:
                details = json.dumps(event.get("data", {}), ensure_ascii=False)[:200]

        lines = details.split("\n")
        first = lines[0]
        rest = "\n".join(f"{'':<{indent}}{l}" for l in lines[1:])

        if use_color:
            color = _ANSI.get(etype, "")
            output = f"{_DIM}{ts}{_RESET}  {color}{label}{_RESET}  {first}"
        else:
            output = f"{ts}  {label}  {first}"
        if rest:
            output += "\n" + rest

        print(output, flush=True)


# ── Rich Live full-screen TUI mode ──────────────────────────────────────────

def run_live(sock: socket.socket, filter_key: str | None, full: bool = False):
    """Full-screen TUI using Rich. Emoji are replaced with ◆ before rendering
    to avoid terminal width calculation mismatches."""
    import shutil, termios, tty

    console = Console(highlight=False)
    events: list[dict] = []
    counts: dict[str, int] = {}

    _RICH_STYLES = {
        "telegram_in":   "bold cyan",
        "telegram_out":  "bold blue",
        "api_request":   "bold yellow",
        "api_response":  "bold green",
        "shell_exec":    "bold magenta",
        "tool_call":     "bold red",
        "route":         "dim",
    }

    def render():
        term = shutil.get_terminal_size((120, 40))
        max_rows = max(3, term.lines - 5)

        # Stats for title
        stat_parts = []
        for t, c in sorted(counts.items()):
            style = _RICH_STYLES.get(t, "dim")
            stat_parts.append(f"[{style}]{_label(t).strip()}:{c}[/]")
        stats_text = "  ".join(stat_parts) if stat_parts else "waiting for events..."

        table = Table(box=box.SIMPLE, expand=True, show_header=True,
                      header_style="bold", padding=(0, 1))
        table.add_column("Time", width=12, no_wrap=True)
        table.add_column("Type", width=_LABEL_WIDTH, no_wrap=True)
        table.add_column("Details", ratio=1, no_wrap=True, overflow="ellipsis")

        visible = events[-max_rows:]
        for ev in visible:
            ts = _ts(ev.get("ts", 0))
            etype = ev.get("type", "?")
            style = _RICH_STYLES.get(etype, "dim")
            label = _label(etype)

            if full:
                details = json.dumps(ev.get("data", {}), ensure_ascii=False)
            else:
                formatter = _FORMATTERS.get(etype)
                if formatter:
                    details = formatter(ev.get("data", {})).replace("\n", "  ")
                else:
                    details = json.dumps(ev.get("data", {}), ensure_ascii=False)
            details = _strip_emoji(details)

            table.add_row(
                Text(ts, style="dim"),
                Text(label, style=style),
                Text(details),
            )

        return Panel(
            table,
            title=f"[bold]Bot Debug Monitor[/]  │  filter: [yellow]{filter_key or 'all'}[/]  │  {stats_text}",
            subtitle="[dim]Ctrl+C to quit  │  --full for complete data  │  --filter <api|tg|shell|tool|route>[/]",
            border_style="bright_blue",
            height=term.lines,
        )

    fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        with Live(render(), console=console, refresh_per_second=2, screen=True) as live:
            for event in iter_events(sock):
                etype = event.get("type", "")
                if not _matches_filter(etype, filter_key):
                    continue
                events.append(event)
                counts[etype] = counts.get(etype, 0) + 1
                if len(events) > 200:
                    events[:] = events[-100:]
                live.update(render())
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
    console.print("[dim]Disconnected.[/]")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Bot debug monitor")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--filter", dest="filter_key", default=None,
                        help="Filter: api, tg, shell, tool, route")
    parser.add_argument("--raw", action="store_true",
                        help="Output raw JSON Lines")
    parser.add_argument("--live", action="store_true",
                        help="Full-screen TUI mode (Rich panel)")
    parser.add_argument("--full", action="store_true",
                        help="Show complete event data (not truncated)")
    parser.add_argument("--no-color", action="store_true",
                        help="Disable ANSI colors in streaming mode")
    args = parser.parse_args()

    print(f"Connecting to {args.host}:{args.port}...", file=sys.stderr)
    try:
        sock = connect(args.host, args.port)
    except ConnectionRefusedError:
        print(f"Error: cannot connect to {args.host}:{args.port}. Is the bot running?",
              file=sys.stderr)
        sys.exit(1)
    print("Connected. Waiting for events... (Ctrl+C to quit)", file=sys.stderr)

    try:
        if args.raw:
            run_raw(sock, args.filter_key)
        elif args.live:
            run_live(sock, args.filter_key, full=args.full)
        else:
            use_color = not args.no_color and sys.stdout.isatty()
            run_stream(sock, args.filter_key, full=args.full, use_color=use_color)
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
        print("\nDisconnected.", file=sys.stderr)


if __name__ == "__main__":
    main()
