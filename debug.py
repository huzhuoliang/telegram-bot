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

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.syntax import Syntax
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

class _TreeRow:
    """A row in the detail tree view."""
    __slots__ = ("key", "value", "depth", "is_nested", "expanded", "is_last")

    def __init__(self, key: str, value: object, depth: int, is_nested: bool):
        self.key = key
        self.value = value
        self.depth = depth
        self.is_nested = is_nested
        self.expanded = False
        self.is_last = False  # set by _update_tree_lines after expand/collapse

    @property
    def display_value(self) -> str:
        return _value_preview(self.value)


def _make_tree_rows(data, depth: int = 0) -> list[_TreeRow]:
    """Build top-level tree rows from a dict or list."""
    rows: list[_TreeRow] = []
    if isinstance(data, dict):
        for k, v in data.items():
            rows.append(_TreeRow(k, v, depth, isinstance(v, (dict, list))))
    elif isinstance(data, list):
        for i, item in enumerate(data):
            rows.append(_TreeRow(f"[{i}]", item, depth, isinstance(item, (dict, list))))
    _update_is_last(rows)
    return rows


def _update_is_last(rows: list[_TreeRow]):
    """Mark each row's is_last flag: True if it's the last sibling at its depth.
    This determines whether to draw └ or ├ in the tree lines."""
    for i, row in enumerate(rows):
        # Look forward for the next row at the same or lesser depth
        row.is_last = True
        for j in range(i + 1, len(rows)):
            if rows[j].depth < row.depth:
                break  # parent ended, we are last
            if rows[j].depth == row.depth:
                row.is_last = False  # there's a sibling after us
                break


def _tree_prefix(row: _TreeRow, rows: list[_TreeRow], idx: int) -> str:
    """Build the tree-line prefix for a row, e.g. '│  ├▸ '."""
    if row.depth == 0:
        if row.is_nested:
            return "▾ " if row.expanded else "▸ "
        return "  "

    # Build the guide lines for each ancestor depth
    parts = []
    for d in range(1, row.depth):
        # Check if there's a non-last ancestor at this depth
        has_continuation = False
        for k in range(idx - 1, -1, -1):
            if rows[k].depth == d:
                has_continuation = not rows[k].is_last
                break
            if rows[k].depth < d:
                break
        parts.append("│  " if has_continuation else "   ")

    # The connector for this row's own depth
    connector = "└─ " if row.is_last else "├─ "
    parts.append(connector)

    # Expand/collapse icon for nested rows
    if row.is_nested:
        parts.append("▾ " if row.expanded else "▸ ")
    else:
        parts.append("")

    return "".join(parts)


def _expand_row(rows: list[_TreeRow], idx: int):
    """Expand a nested row: insert its children below it."""
    row = rows[idx]
    if not row.is_nested or row.expanded:
        return
    row.expanded = True
    children = _make_tree_rows(row.value, row.depth + 1)
    rows[idx + 1:idx + 1] = children
    _update_is_last(rows)


def _collapse_row(rows: list[_TreeRow], idx: int):
    """Collapse a nested row: remove all descendants below it."""
    row = rows[idx]
    if not row.is_nested or not row.expanded:
        return
    row.expanded = False
    remove_end = idx + 1
    while remove_end < len(rows) and rows[remove_end].depth > row.depth:
        remove_end += 1
    del rows[idx + 1:remove_end]
    _update_is_last(rows)


def _sanitize_line(s: str) -> str:
    """Replace all control characters that would break single-line display."""
    import re
    return re.sub(r'[\x00-\x1f\x7f\x85\x2028\x2029]+', ' ', s)


def _value_preview(v, max_len: int = 120) -> str:
    """Short single-line preview string for a JSON value."""
    if isinstance(v, dict):
        return f"{{{len(v)} fields}}"
    if isinstance(v, list):
        return f"[{len(v)} items]"
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    s = _sanitize_line(str(v))
    if len(s) > max_len:
        return s[:max_len] + "..."
    return s


def run_live(sock: socket.socket, filter_key: str | None, full: bool = False):
    """Full-screen TUI using Rich with interactive navigation and mouse support.

    List view:    ↑↓/click navigate, Enter/dblclick view detail
    Detail view:  ↑↓/click navigate fields, Enter drill into nested / view leaf
    Value view:   ↑↓/scroll text, Esc back
    Esc goes back one level; Esc from list quits.
    """
    import os, select, shutil, termios, tty

    console = Console(highlight=False)
    events: list[dict] = []
    counts: dict[str, int] = {}

    # ── View stack ───────────────────────────────────────────────────────
    # Each entry: (view_type, state_dict)
    # view_type: "list" | "detail" | "value"
    VIEW_LIST = "list"
    VIEW_DETAIL = "detail"
    VIEW_VALUE = "value"

    view_stack: list[tuple[str, dict]] = []
    view_mode = VIEW_LIST

    # List state
    selected = 0
    auto_follow = True

    # Detail state (reused across stack levels)
    detail_event = None
    detail_data = None       # the dict/list currently being browsed
    detail_rows = []         # one-level flattened rows
    detail_sel = 0
    detail_scroll = 0
    detail_title = ""        # breadcrumb path

    # Value state
    value_text = ""
    value_title = ""
    value_scroll = 0

    _RICH_STYLES = {
        "telegram_in":   "bold cyan",
        "telegram_out":  "bold blue",
        "api_request":   "bold yellow",
        "api_response":  "bold green",
        "shell_exec":    "bold magenta",
        "tool_call":     "bold red",
        "route":         "dim",
    }

    # Search state
    search_term = ""       # current search keyword
    search_input = False   # True when reading search input

    # Row offset for mouse click mapping (set during render)
    _list_row_offset = 5    # rows before first event: border(1) + header(1) + separator(1) + column header(1) + col separator(1)
    _detail_row_offset = 3  # rows before first field in detail view
    _detail_click_map: list[int] = []  # terminal_row -> data index, built during render

    def _max_rows():
        return max(3, shutil.get_terminal_size((120, 40)).lines - 6)

    def _visible():
        return events[-_max_rows():]

    def _enter_detail(ev: dict, data, title: str):
        nonlocal view_mode, detail_event, detail_data, detail_rows, detail_sel, detail_scroll, detail_title
        detail_event = ev
        detail_data = data
        detail_rows = _make_tree_rows(data)
        detail_sel = 0
        detail_scroll = 0
        detail_title = title
        view_mode = VIEW_DETAIL

    def _enter_value(text: str, title: str):
        nonlocal view_mode, value_text, value_title, value_scroll
        value_text = text
        value_title = title
        value_scroll = 0
        view_mode = VIEW_VALUE

    def _push_and_enter_detail(ev: dict, data, title: str):
        """Push current detail state onto stack, then enter new detail."""
        view_stack.append((VIEW_DETAIL, {
            "event": detail_event, "data": detail_data,
            "rows": detail_rows, "sel": detail_sel,
            "scroll": detail_scroll, "title": detail_title,
        }))
        _enter_detail(ev, data, title)

    def _pop_view():
        nonlocal view_mode, detail_event, detail_data, detail_rows, detail_sel, detail_scroll, detail_title
        nonlocal value_text, value_title, value_scroll
        if not view_stack:
            view_mode = VIEW_LIST
            return
        prev_type, prev_state = view_stack.pop()
        if prev_type == VIEW_DETAIL:
            detail_event = prev_state["event"]
            detail_data = prev_state["data"]
            detail_rows = prev_state["rows"]
            detail_sel = prev_state["sel"]
            detail_scroll = prev_state["scroll"]
            detail_title = prev_state["title"]
            view_mode = VIEW_DETAIL
        else:
            view_mode = VIEW_LIST

    # ── Search bar ────────────────────────────────────────────────────────

    def _render_search_bar(width: int) -> Panel:
        prompt = Text()
        prompt.append(" /", style="bold yellow")
        prompt.append(search_buf, style="bold")
        prompt.append("█", style="bold yellow blink")
        return Panel(
            prompt,
            border_style="yellow",
            height=3,
            width=width,
            title="[bold yellow]Search[/]",
            title_align="left",
        )

    # ── List view ────────────────────────────────────────────────────────

    def render_list():
        term = shutil.get_terminal_size((120, 40))
        max_rows = _max_rows()

        stat_parts = []
        for t, c in sorted(counts.items()):
            style = _RICH_STYLES.get(t, "dim")
            stat_parts.append(f"[{style}]{_label(t).strip()}:{c}[/]")
        stats_text = "  ".join(stat_parts) if stat_parts else "waiting for events..."

        table = Table(box=box.SIMPLE, expand=True, show_header=True,
                      header_style="bold", padding=(0, 1))
        table.add_column("", width=1, no_wrap=True)
        table.add_column("Time", width=12, no_wrap=True)
        table.add_column("Type", width=_LABEL_WIDTH, no_wrap=True)
        table.add_column("Details", ratio=1, no_wrap=True, overflow="ellipsis")

        visible = _visible()
        for i, ev in enumerate(visible):
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

            is_sel = (i == selected)
            details_text = _highlight_text(details, search_term) if search_term else Text(details)
            table.add_row(
                Text("▸" if is_sel else " ", style="bold yellow" if is_sel else ""),
                Text(ts, style="dim" if not is_sel else ""),
                Text(label, style=style if not is_sel else ""),
                details_text,
                style="reverse" if is_sel else "",
            )

        follow = " [green]●[/] auto" if auto_follow else " [dim]○[/] manual"
        if search_term and not search_input:
            sub = f"[dim]↑↓/hjkl select  Enter detail  / search  Esc quit[/]{follow}  [yellow]/{search_term}[/] n/N"
        else:
            sub = f"[dim]↑↓/hjkl select  Enter detail  / search  Esc quit[/]{follow}"
        panel_h = term.lines - (3 if search_input else 0)
        panel = Panel(
            table,
            title=f"[bold]Bot Debug Monitor[/]  │  filter: [yellow]{filter_key or 'all'}[/]  │  {stats_text}",
            subtitle=sub,
            border_style="bright_blue",
            height=panel_h,
        )
        if search_input:
            return Group(panel, _render_search_bar(term.columns))
        return panel

    # ── Detail view ──────────────────────────────────────────────────────

    def render_detail():
        nonlocal detail_scroll
        term = shutil.get_terminal_size((120, 40))
        ev = detail_event
        etype = ev.get("type", "?")
        ts = _ts(ev.get("ts", 0))
        style = _RICH_STYLES.get(etype, "dim")
        content_h = max(3, term.lines - 6)

        if detail_sel < detail_scroll:
            detail_scroll = detail_sel
        elif detail_sel >= detail_scroll + content_h:
            detail_scroll = detail_sel - content_h + 1

        table = Table(box=None, expand=True, show_header=True,
                      header_style="bold", padding=(0, 1))
        table.add_column("", width=1, no_wrap=True)
        table.add_column("Field", min_width=20, max_width=40, no_wrap=True)
        table.add_column("Value", ratio=1, no_wrap=True, overflow="ellipsis")

        _detail_click_map.clear()
        visible_rows = detail_rows[detail_scroll:detail_scroll + content_h]
        for i, trow in enumerate(visible_rows):
            abs_i = detail_scroll + i
            is_sel = (abs_i == detail_sel)
            preview = _strip_emoji(trow.display_value)
            marker = "▸" if is_sel else " "
            prefix = _tree_prefix(trow, detail_rows, abs_i)
            field_text = _strip_emoji(f"{prefix}{trow.key}")
            _detail_click_map.append(abs_i)
            field_display = _highlight_text(field_text, search_term) if search_term else Text(field_text, style="bold" if is_sel else "dim")
            value_display = _highlight_text(preview, search_term) if search_term else Text(preview)
            table.add_row(
                Text(marker, style="bold yellow" if is_sel else ""),
                field_display,
                value_display,
                style="reverse" if is_sel else "",
            )

        breadcrumb = detail_title or "data"
        depth = len(view_stack)
        pos = f"  {detail_sel + 1}/{len(detail_rows)}" if detail_rows else ""
        if search_term and not search_input:
            sub = f"[dim]↑↓/hjkl navigate  ←→ collapse/expand  Enter drill  / search  Esc back ({depth})[/]  [yellow]/{search_term}[/] n/N"
        else:
            sub = f"[dim]↑↓/hjkl navigate  ←→ collapse/expand  Enter drill  / search  Esc back ({depth})[/]"
        panel_h = term.lines - (3 if search_input else 0)
        panel = Panel(
            table,
            title=f"[bold]{breadcrumb}[/]  │  [{style}]{etype}[/]  {ts}{pos}",
            subtitle=sub,
            border_style="yellow",
            height=panel_h,
        )
        if search_input:
            return Group(panel, _render_search_bar(term.columns))
        return panel

    # ── Value view ───────────────────────────────────────────────────────

    def render_value():
        nonlocal value_scroll
        term = shutil.get_terminal_size((120, 40))
        panel_h = term.lines - (3 if search_input else 0)
        content_h = max(3, panel_h - 5)

        text = _strip_emoji(value_text)
        try:
            parsed = json.loads(text)
            text = json.dumps(parsed, ensure_ascii=False, indent=2)
            is_json = True
        except (json.JSONDecodeError, TypeError):
            is_json = False

        lines = text.split("\n")
        total = len(lines)
        max_scroll = max(0, total - content_h)
        value_scroll = min(value_scroll, max_scroll)
        visible = lines[value_scroll:value_scroll + content_h]

        if search_term and not is_json:
            # Plain text: highlight search matches
            content = Text()
            for i, line in enumerate(visible):
                if i > 0:
                    content.append("\n")
                content.append_text(_highlight_text(line, search_term))
        elif search_term and is_json:
            # JSON: can't use Syntax with highlights, fall back to Text with highlights
            content = Text()
            for i, line in enumerate(visible):
                if i > 0:
                    content.append("\n")
                content.append_text(_highlight_text(line, search_term))
        elif is_json:
            content = Syntax("\n".join(visible), "json",
                             theme="ansi_dark", line_numbers=False, word_wrap=False)
        else:
            content = Text("\n".join(visible))

        pos = f"  lines {value_scroll+1}-{min(value_scroll+content_h, total)}/{total}"
        if search_term and not search_input:
            # Count matches
            lower = search_term.lower()
            match_lines = [i for i, l in enumerate(lines) if lower in l.lower()]
            match_info = f"  [yellow]/{search_term}[/] {len(match_lines)} matches  n/N"
            sub = f"[dim]↑↓/jk scroll  / search  Esc back[/]{match_info}"
        else:
            sub = "[dim]↑↓/jk scroll  / search  Esc back[/]"
        panel = Panel(
            content,
            title=f"[bold]{_strip_emoji(value_title)}[/]{pos}",
            subtitle=sub,
            border_style="green",
            height=panel_h,
        )
        if search_input:
            return Group(panel, _render_search_bar(term.columns))
        return panel

    # ── Input reading (keyboard + mouse) ─────────────────────────────────

    def _read_input(fd: int) -> tuple[str | None, int, int]:
        """Returns (action, row, col). row/col only meaningful for mouse events.
        Actions: 'up','down','enter','esc','backspace','q','click','scroll_up','scroll_down' or None."""
        if not select.select([fd], [], [], 0)[0]:
            return None, 0, 0
        ch = os.read(fd, 1)
        if ch == b'\x1b':
            if not select.select([fd], [], [], 0.02)[0]:
                return 'esc', 0, 0
            ch2 = os.read(fd, 1)
            if ch2 == b'[':
                # Read until we have a complete sequence
                buf = b""
                while select.select([fd], [], [], 0.02)[0]:
                    b = os.read(fd, 1)
                    buf += b
                    # Arrow keys: single letter
                    if b == b'A': return 'up', 0, 0
                    if b == b'B': return 'down', 0, 0
                    if b == b'C': return 'right', 0, 0
                    if b == b'D': return 'left', 0, 0
                    if b == b'H': return 'home', 0, 0
                    if b == b'F': return 'end', 0, 0
                    # Tilde-terminated: ESC[5~ PgUp, ESC[6~ PgDn, ESC[1~ Home, ESC[4~ End
                    if b == b'~':
                        num = buf[:-1]  # everything before ~
                        if num == b'5': return 'pgup', 0, 0
                        if num == b'6': return 'pgdn', 0, 0
                        if num == b'1': return 'home', 0, 0
                        if num == b'4': return 'end', 0, 0
                        return None, 0, 0
                    # SGR mouse: ESC [ < ... M or ... m
                    if b in (b'M', b'm'):
                        # Parse SGR mouse: <btn;col;row M/m
                        try:
                            parts = buf[1:-1].decode()  # skip '<', strip M/m
                            segs = parts.split(";")
                            btn = int(segs[0])
                            col = int(segs[1])
                            row = int(segs[2])
                            is_release = (b == b'm')
                            if btn == 0 and is_release:  # left click release
                                return 'click', row, col
                            if btn == 64:  # scroll up
                                return 'scroll_up', row, col
                            if btn == 65:  # scroll down
                                return 'scroll_down', row, col
                        except (ValueError, IndexError):
                            pass
                        return None, 0, 0
                return None, 0, 0
            # Drain
            while select.select([fd], [], [], 0.01)[0]:
                os.read(fd, 1)
            return None, 0, 0
        if ch in (b'\r', b'\n'): return 'enter', 0, 0
        if ch in (b'\x7f', b'\x08'): return 'backspace', 0, 0
        if ch in (b'q', b'Q'): return 'q', 0, 0
        if ch == b'k': return 'up', 0, 0
        if ch == b'j': return 'down', 0, 0
        if ch == b'h': return 'left', 0, 0
        if ch == b'l': return 'right', 0, 0
        if ch == b'/': return 'search', 0, 0
        if ch == b'n': return 'search_next', 0, 0
        if ch == b'N': return 'search_prev', 0, 0
        return None, 0, 0

    # ── Search helpers ────────────────────────────────────────────────────

    # Search input buffer (used when search_input mode is active)
    search_buf = ""

    def _highlight_text(text: str, term: str) -> Text:
        """Create a Rich Text with search term highlighted."""
        if not term:
            return Text(text)
        result = Text()
        lower_text = text.lower()
        lower_term = term.lower()
        i = 0
        while i < len(text):
            pos = lower_text.find(lower_term, i)
            if pos == -1:
                result.append(text[i:])
                break
            if pos > i:
                result.append(text[i:pos])
            result.append(text[pos:pos + len(term)], style="black on yellow")
            i = pos + len(term)
        return result

    def _find_matches_list(term: str, events_list: list[dict]) -> list[int]:
        """Find indices in events_list that match the search term."""
        if not term:
            return []
        lower = term.lower()
        return [i for i, ev in enumerate(events_list)
                if lower in json.dumps(ev.get("data", {}), ensure_ascii=False).lower()
                or lower in ev.get("type", "").lower()]

    def _find_matches_detail(term: str, rows: list) -> list[int]:
        """Find indices in detail_rows that match the search term."""
        if not term:
            return []
        lower = term.lower()
        return [i for i, trow in enumerate(rows)
                if lower in trow.key.lower() or lower in str(trow.value).lower()]

    # ── Main loop ────────────────────────────────────────────────────────

    sock.setblocking(False)
    sock_buf = b""
    fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(fd)

    # SGR mouse mode: works over SSH in Windows Terminal
    MOUSE_ON = "\033[?1000h\033[?1006h"   # enable button events + SGR encoding
    MOUSE_OFF = "\033[?1000l\033[?1006l"

    try:
        tty.setcbreak(fd)
        sys.stdout.write(MOUSE_ON)
        sys.stdout.flush()

        def _read_raw_char(fd: int) -> bytes | None:
            """Read a single raw byte if available."""
            if not select.select([fd], [], [], 0)[0]:
                return None
            return os.read(fd, 1)

        def _handle_search_char(ch: bytes) -> bool:
            """Process a char during search input. Returns True if search mode ended."""
            nonlocal search_input, search_buf, search_term
            if ch in (b'\r', b'\n'):
                # Confirm search
                search_term = search_buf
                search_input = False
                return True
            if ch == b'\x1b':
                # Cancel search
                while select.select([fd], [], [], 0.02)[0]:
                    os.read(fd, 1)
                search_buf = ""
                search_input = False
                return True
            if ch in (b'\x7f', b'\x08'):
                search_buf = search_buf[:-1]
                return False
            try:
                search_buf += ch.decode("utf-8", errors="ignore")
            except Exception:
                pass
            return False

        def _apply_search_list():
            nonlocal selected, auto_follow
            if search_term:
                matches = _find_matches_list(search_term, _visible())
                if matches:
                    auto_follow = False
                    selected = matches[0]

        def _apply_search_detail():
            nonlocal detail_sel
            if search_term:
                matches = _find_matches_detail(search_term, detail_rows)
                if matches:
                    detail_sel = matches[0]

        def _get_value_lines() -> list[str]:
            """Get the processed value text lines (same logic as render_value)."""
            text = _strip_emoji(value_text)
            try:
                parsed = json.loads(text)
                text = json.dumps(parsed, ensure_ascii=False, indent=2)
            except (json.JSONDecodeError, TypeError):
                pass
            return text.split("\n")

        def _apply_search_value():
            nonlocal value_scroll
            if search_term:
                lower = search_term.lower()
                for i, line in enumerate(_get_value_lines()):
                    if lower in line.lower():
                        value_scroll = i
                        return

        def _value_search_jump(direction: int):
            """Jump to next (direction=1) or prev (direction=-1) search match in value view."""
            nonlocal value_scroll
            lower = search_term.lower()
            lines = _get_value_lines()
            if direction > 0:
                for i in range(value_scroll + 1, len(lines)):
                    if lower in lines[i].lower():
                        value_scroll = i
                        return
                # Wrap around
                for i in range(0, value_scroll):
                    if lower in lines[i].lower():
                        value_scroll = i
                        return
            else:
                for i in range(value_scroll - 1, -1, -1):
                    if lower in lines[i].lower():
                        value_scroll = i
                        return
                for i in range(len(lines) - 1, value_scroll, -1):
                    if lower in lines[i].lower():
                        value_scroll = i
                        return

        with Live(render_list(), console=console, auto_refresh=False, screen=True) as live:
            live.refresh()
            while True:
                need_render = False

                # ── Search input mode: raw char processing ──
                if search_input:
                    ch = _read_raw_char(fd)
                    if ch:
                        ended = _handle_search_char(ch)
                        need_render = True
                        if ended and search_term:
                            if view_mode == VIEW_LIST:
                                _apply_search_list()
                            elif view_mode == VIEW_DETAIL:
                                _apply_search_detail()
                            elif view_mode == VIEW_VALUE:
                                _apply_search_value()
                else:
                    # ── Normal key processing ──
                    while True:
                        action, row, col = _read_input(fd)
                        if not action:
                            break
                        need_render = True

                        if view_mode == VIEW_LIST:
                            visible = _visible()
                            if action in ('up', 'scroll_up'):
                                auto_follow = False
                                selected = max(0, selected - 1)
                            elif action in ('down', 'scroll_down'):
                                if selected < len(visible) - 1:
                                    selected += 1
                                else:
                                    auto_follow = True
                            elif action == 'home':
                                auto_follow = False
                                selected = 0
                            elif action == 'end':
                                selected = max(0, len(visible) - 1)
                                auto_follow = True
                            elif action == 'pgup':
                                auto_follow = False
                                selected = max(0, selected - _max_rows())
                            elif action == 'pgdn':
                                selected = min(max(0, len(visible) - 1), selected + _max_rows())
                                if selected >= len(visible) - 1:
                                    auto_follow = True
                            elif action == 'click':
                                idx = row - _list_row_offset
                                if 0 <= idx < len(visible):
                                    auto_follow = False
                                    if selected == idx:
                                        detail_event = visible[selected]
                                        view_stack.clear()
                                        _enter_detail(detail_event, detail_event.get("data", {}), "data")
                                    else:
                                        selected = idx
                            elif action == 'enter':
                                if visible and 0 <= selected < len(visible):
                                    detail_event = visible[selected]
                                    view_stack.clear()
                                    _enter_detail(detail_event, detail_event.get("data", {}), "data")
                            elif action == 'search':
                                search_input = True
                                search_buf = ""
                                break  # exit key loop, re-enter as search input mode
                            elif action == 'search_next':
                                if search_term:
                                    matches = _find_matches_list(search_term, _visible())
                                    after = [m for m in matches if m > selected]
                                    selected = after[0] if after else (matches[0] if matches else selected)
                                    auto_follow = False
                            elif action == 'search_prev':
                                if search_term:
                                    matches = _find_matches_list(search_term, _visible())
                                    before = [m for m in matches if m < selected]
                                    selected = before[-1] if before else (matches[-1] if matches else selected)
                                    auto_follow = False
                            elif action in ('esc', 'q'):
                                if search_term:
                                    search_term = ""
                                else:
                                    sys.stdout.write(MOUSE_OFF)
                                    sys.stdout.flush()
                                    termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
                                    return

                        elif view_mode == VIEW_DETAIL:
                            if action in ('up', 'scroll_up'):
                                detail_sel = max(0, detail_sel - 1)
                            elif action in ('down', 'scroll_down'):
                                detail_sel = min(len(detail_rows) - 1, detail_sel + 1)
                            elif action == 'home':
                                detail_sel = 0
                            elif action == 'end':
                                detail_sel = max(0, len(detail_rows) - 1)
                            elif action == 'pgup':
                                detail_sel = max(0, detail_sel - _max_rows())
                            elif action == 'pgdn':
                                detail_sel = min(max(0, len(detail_rows) - 1), detail_sel + _max_rows())
                            elif action == 'right':
                                if detail_rows and 0 <= detail_sel < len(detail_rows):
                                    trow = detail_rows[detail_sel]
                                    if trow.is_nested and not trow.expanded:
                                        _expand_row(detail_rows, detail_sel)
                            elif action == 'left':
                                if detail_rows and 0 <= detail_sel < len(detail_rows):
                                    trow = detail_rows[detail_sel]
                                    if trow.is_nested and trow.expanded:
                                        _collapse_row(detail_rows, detail_sel)
                                    elif trow.depth > 0:
                                        parent_depth = trow.depth - 1
                                        for pi in range(detail_sel - 1, -1, -1):
                                            if detail_rows[pi].depth == parent_depth and detail_rows[pi].is_nested:
                                                detail_sel = pi
                                                break
                            elif action == 'click':
                                map_idx = row - _detail_row_offset
                                if 0 <= map_idx < len(_detail_click_map):
                                    abs_idx = _detail_click_map[map_idx]
                                    if detail_sel == abs_idx:
                                        action = 'enter'
                                    else:
                                        detail_sel = abs_idx
                            elif action == 'search':
                                search_input = True
                                search_buf = ""
                                break
                            elif action == 'search_next':
                                if search_term:
                                    matches = _find_matches_detail(search_term, detail_rows)
                                    after = [m for m in matches if m > detail_sel]
                                    detail_sel = after[0] if after else (matches[0] if matches else detail_sel)
                            elif action == 'search_prev':
                                if search_term:
                                    matches = _find_matches_detail(search_term, detail_rows)
                                    before = [m for m in matches if m < detail_sel]
                                    detail_sel = before[-1] if before else (matches[-1] if matches else detail_sel)
                            elif action in ('esc', 'backspace'):
                                if search_term:
                                    search_term = ""
                                else:
                                    _pop_view()

                            if action == 'enter':
                                if detail_rows and 0 <= detail_sel < len(detail_rows):
                                    trow = detail_rows[detail_sel]
                                    path = f"{detail_title}.{trow.key}" if detail_title else trow.key
                                    if trow.is_nested and isinstance(trow.value, (dict, list)):
                                        _push_and_enter_detail(detail_event, trow.value, path)
                                    else:
                                        sv = json.dumps(trow.value, ensure_ascii=False, indent=2) if isinstance(trow.value, (dict, list)) else str(trow.value)
                                        view_stack.append((VIEW_DETAIL, {
                                            "event": detail_event, "data": detail_data,
                                            "rows": detail_rows, "sel": detail_sel,
                                            "scroll": detail_scroll, "title": detail_title,
                                        }))
                                        _enter_value(sv, path)

                        elif view_mode == VIEW_VALUE:
                            if action in ('up', 'scroll_up'):
                                value_scroll = max(0, value_scroll - 1)
                            elif action in ('down', 'scroll_down'):
                                value_scroll += 1
                            elif action == 'home':
                                value_scroll = 0
                            elif action == 'end':
                                value_scroll = 999999  # clamped in render_value
                            elif action == 'pgup':
                                value_scroll = max(0, value_scroll - _max_rows())
                            elif action == 'pgdn':
                                value_scroll += _max_rows()  # clamped in render_value
                            elif action == 'search':
                                search_input = True
                                search_buf = ""
                                break
                            elif action == 'search_next':
                                if search_term:
                                    _value_search_jump(1)
                            elif action == 'search_prev':
                                if search_term:
                                    _value_search_jump(-1)
                            elif action in ('esc', 'backspace'):
                                if search_term:
                                    search_term = ""
                                else:
                                    _pop_view()
                            elif action in ('enter', 'q'):
                                _pop_view()

                # Read socket (EOF just stops reading, doesn't exit — for demo mode)
                sock_alive = True
                try:
                    chunk = sock.recv(4096)
                    if not chunk:
                        sock_alive = False
                    else:
                        sock_buf += chunk
                except BlockingIOError:
                    pass
                except (ConnectionResetError, OSError):
                    sock_alive = False

                while b"\n" in sock_buf:
                    line, sock_buf = sock_buf.split(b"\n", 1)
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    etype = event.get("type", "")
                    if not _matches_filter(etype, filter_key):
                        continue
                    events.append(event)
                    counts[etype] = counts.get(etype, 0) + 1
                    if len(events) > 500:
                        events[:] = events[-250:]
                    if view_mode == VIEW_LIST:
                        need_render = True

                if need_render:
                    if view_mode == VIEW_LIST:
                        if auto_follow:
                            selected = max(0, len(_visible()) - 1)
                        live.update(render_list())
                    elif view_mode == VIEW_DETAIL:
                        live.update(render_detail())
                    elif view_mode == VIEW_VALUE:
                        live.update(render_value())
                    live.refresh()

                wait_fds = [fd, sock] if sock_alive else [fd]
                select.select(wait_fds, [], [], 0.03)

    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(MOUSE_OFF)
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
    console.print("[dim]Disconnected.[/]")


# ── Demo data ────────────────────────────────────────────────────────────────

def _generate_demo_events() -> list[dict]:
    """Generate realistic sample events for --demo mode."""
    import time as _time
    t = _time.time()
    return [
        {"ts": t - 30, "type": "telegram_in", "data": {
            "message": {"chat": {"id": 12345}, "from": {"first_name": "Alice"},
                        "text": "$统计一下磁盘使用情况，多用 emoji 表情 🖥️💾📊"}}},
        {"ts": t - 29.9, "type": "route", "data": {
            "handler": "privileged_claude", "reason": "$ prefix",
            "text": "$统计一下磁盘使用情况"}},
        {"ts": t - 29, "type": "telegram_out", "data": {
            "method": "sendMessage", "payload": {"text": "⏳ 处理中..."}, "status": 200}},
        {"ts": t - 28.8, "type": "api_request", "data": {
            "model": "claude-sonnet-4-6", "max_tokens": 4096, "round": 0,
            "system": "You are a helpful assistant running as a Telegram bot on the user's personal Linux server...(1769 chars)",
            "messages": [
                {"role": "user", "content": "统计一下磁盘使用情况，多用emoji表情"},
                {"role": "assistant", "content": "好的，让我查看磁盘信息。"},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_01", "content": "exit 0\nFilesystem  Size  Used  Avail  Use%  Mounted on\n/dev/sda1  98G  34G  60G  37%  /\ntmpfs  4.7G  2.2M  4.7G  1%  /run"}]},
            ],
            "tools": ["run_shell_command", "read_file", "write_file"]}},
        {"ts": t - 25, "type": "api_response", "data": {
            "stop_reason": "tool_use", "round": 0,
            "text": "让我先查看磁盘使用情况 🔍",
            "usage": {"input_tokens": 1850, "output_tokens": 120},
            "tool_calls": [{"name": "run_shell_command", "input": {"command": "df -h && echo '---' && lsblk"}}]}},
        {"ts": t - 24, "type": "tool_call", "data": {
            "name": "run_shell_command",
            "input": {"command": "df -h && echo '---' && lsblk"},
            "result": "exit 0\nFilesystem                         Size  Used Avail Use% Mounted on\ntmpfs                              4.7G  2.2M  4.7G   1% /run\n/dev/mapper/ubuntu--vg-ubuntu--lv   98G   34G   60G  37% /\ntmpfs                              4.7G     0  4.7G   0% /dev/shm\ntmpfs                              5.0M     0  5.0M   0% /run/lock\n/dev/sda2                          2.0G  253M  1.6G  14% /boot\n---\nNAME                      MAJ:MIN RM  SIZE RO TYPE MOUNTPOINTS\nsda                         8:0    0  100G  0 disk\n├─sda1                      8:1    0    1M  0 part\n├─sda2                      8:2    0    2G  0 part /boot\n└─sda3                      8:3    0   98G  0 part\n  └─ubuntu--vg-ubuntu--lv 253:0    0   98G  0 lvm  /"}},
        {"ts": t - 23, "type": "shell_exec", "data": {
            "command": "df -h && echo '---' && lsblk",
            "output": "exit 0\nFilesystem  Size  Used Avail Use% Mounted on\n/dev/sda1  98G  34G  60G  37%  /",
            "exit_code": 0, "handler": "privileged"}},
        {"ts": t - 20, "type": "api_request", "data": {
            "model": "claude-sonnet-4-6", "max_tokens": 4096, "round": 1,
            "system": "You are a helpful assistant...",
            "messages": [{"role": "user", "content": "统计磁盘"}, {"role": "assistant", "content": "..."}],
            "tools": ["run_shell_command", "read_file", "write_file"]}},
        {"ts": t - 15, "type": "api_response", "data": {
            "stop_reason": "end_turn", "round": 1,
            "text": "以下是你的服务器磁盘使用情况 💽✨\n\n🗂️ 本地磁盘分区\n🟢 系统根目录 /\n├ 💾 总大小：98 GB\n├ 🔴 已使用：34 GB（37%）\n├ 🟢 可用：60 GB\n\n📊 总结：磁盘使用率 37%，空间充足。",
            "usage": {"input_tokens": 3200, "output_tokens": 580}}},
        {"ts": t - 14, "type": "telegram_out", "data": {
            "method": "editMessageText",
            "payload": {"text": "以下是你的服务器磁盘使用情况 💽✨\n\n<b>🗂️ 本地磁盘分区</b>\n🟢 <b>系统根目录</b> <code>/</code>\n├ 💾 总大小：<code>98 GB</code>\n├ 🔴 已使用：<code>34 GB</code>（37%）\n├ 🟢 可用：<code>60 GB</code>\n\n<b>📊 总结</b>：磁盘使用率 37%，空间充足。\n\n<code>━━━\n📊 in 3,200 · out 580 · ctx 1.6%\n💰 session in 5,050 / out 700</code>",
                        "parse_mode": "HTML"},
            "status": 200}},
        # Large text event for testing scroll
        {"ts": t - 10, "type": "api_response", "data": {
            "stop_reason": "end_turn", "round": 0,
            "text": (
                "# 服务器完整健康检查报告\n\n"
                "## 1. 系统概况\n"
                "操作系统: Ubuntu 22.04.3 LTS (Jammy Jellyfish)\n"
                "内核版本: 6.8.0-60-generic\n"
                "主机名: prod-server-01\n"
                "运行时间: 42 天 3 小时 17 分钟\n"
                "系统负载: 0.15, 0.22, 0.18 (1/5/15 min)\n\n"
                "## 2. CPU 信息\n"
                "型号: Intel Xeon E5-2680 v4 @ 2.40GHz\n"
                "核心数: 8 核 16 线程\n"
                "当前频率: 2.40 GHz (最大 3.30 GHz turbo)\n"
                "CPU 使用率:\n"
                "  用户态: 12.3%\n"
                "  系统态: 3.7%\n"
                "  I/O 等待: 0.5%\n"
                "  空闲: 83.5%\n\n"
                "## 3. 内存使用\n"
                "物理内存总计: 32,768 MB (32 GB)\n"
                "已使用: 18,432 MB (56.3%)\n"
                "可用: 14,336 MB (43.7%)\n"
                "缓存/缓冲: 8,192 MB\n"
                "Swap 总计: 4,096 MB\n"
                "Swap 已使用: 128 MB (3.1%)\n\n"
                "## 4. 磁盘使用详情\n"
                "### 4.1 挂载点\n"
                "| 文件系统 | 大小 | 已用 | 可用 | 使用率 | 挂载点 |\n"
                "| /dev/mapper/ubuntu--vg-ubuntu--lv | 98G | 34G | 60G | 37% | / |\n"
                "| /dev/sda2 | 2.0G | 253M | 1.6G | 14% | /boot |\n"
                "| tmpfs | 4.7G | 2.2M | 4.7G | 1% | /run |\n"
                "| /dev/sdb1 | 500G | 312G | 188G | 63% | /data |\n\n"
                "### 4.2 磁盘 I/O 统计\n"
                "sda: 读取 1.2 MB/s, 写入 3.4 MB/s, IOPS 读 150 写 420\n"
                "sdb: 读取 8.7 MB/s, 写入 12.1 MB/s, IOPS 读 890 写 1250\n\n"
                "### 4.3 inode 使用\n"
                "/dev/mapper/ubuntu--vg-ubuntu--lv: 已用 523,412 / 总计 6,553,600 (8%)\n"
                "/dev/sdb1: 已用 1,847,293 / 总计 32,768,000 (5.6%)\n\n"
                "## 5. 网络状态\n"
                "### 5.1 接口列表\n"
                "eth0: 192.168.1.100/24 (UP, MTU 1500)\n"
                "  MAC: 00:11:22:33:44:55\n"
                "  RX: 156.7 GB (packets: 124,892,341)\n"
                "  TX: 89.3 GB (packets: 67,234,128)\n"
                "  RX errors: 0, TX errors: 0\n"
                "lo: 127.0.0.1/8 (UP, MTU 65536)\n\n"
                "### 5.2 监听端口\n"
                "tcp  0.0.0.0:22    sshd\n"
                "tcp  0.0.0.0:80    nginx\n"
                "tcp  0.0.0.0:443   nginx\n"
                "tcp  127.0.0.1:5432  postgresql\n"
                "tcp  127.0.0.1:6379  redis-server\n"
                "tcp  127.0.0.1:8765  telegram_bot (notify_server)\n"
                "tcp  0.0.0.0:2080  sing-box\n\n"
                "### 5.3 活跃连接\n"
                "ESTABLISHED: 47\n"
                "TIME_WAIT: 12\n"
                "CLOSE_WAIT: 3\n"
                "LISTEN: 8\n\n"
                "## 6. 进程信息\n"
                "### 6.1 TOP 10 CPU 占用\n"
                "PID    USER     %CPU  %MEM  COMMAND\n"
                "1234   www-data  8.2   3.1  nginx: worker process\n"
                "2345   postgres  5.7   12.4  postgres: autovacuum\n"
                "3456   root      3.2   0.8  sing-box run\n"
                "4567   user      2.1   4.5  python3 bot.py\n"
                "5678   redis     1.8   2.3  redis-server *:6379\n"
                "6789   www-data  1.5   2.8  nginx: worker process\n"
                "7890   root      1.2   0.5  sshd: user@pts/0\n"
                "8901   postgres  0.9   8.7  postgres: wal writer\n"
                "9012   root      0.7   0.3  systemd-journald\n"
                "0123   root      0.5   0.2  cron\n\n"
                "### 6.2 服务状态\n"
                "● nginx.service - active (running) since 42 days ago\n"
                "● postgresql.service - active (running) since 42 days ago\n"
                "● redis-server.service - active (running) since 42 days ago\n"
                "● sing-box.service - active (running) since 3 days ago\n"
                "● telegram_bot.service - active (running) since 1 day ago\n"
                "● cron.service - active (running) since 42 days ago\n"
                "● ssh.service - active (running) since 42 days ago\n\n"
                "## 7. 安全检查\n"
                "### 7.1 最近登录\n"
                "user  pts/0  192.168.1.50  Apr  2 14:20  still logged in\n"
                "user  pts/1  192.168.1.50  Apr  1 09:30 - Apr  1 18:45\n"
                "root  tty1   (console)     Mar 28 03:15 - Mar 28 03:20\n\n"
                "### 7.2 失败登录尝试 (最近24小时)\n"
                "总计: 847 次\n"
                "来源 IP 分布:\n"
                "  45.148.10.x: 312 次 (中国)\n"
                "  185.224.128.x: 198 次 (俄罗斯)\n"
                "  103.99.0.x: 156 次 (印度)\n"
                "  其他: 181 次\n"
                "全部被 fail2ban 拦截，无成功入侵。\n\n"
                "### 7.3 防火墙状态\n"
                "UFW: active\n"
                "规则: 允许 22/tcp, 80/tcp, 443/tcp, 2080/tcp\n"
                "默认策略: deny incoming, allow outgoing\n"
                "fail2ban jails: sshd (active, 23 banned IPs)\n\n"
                "## 8. 定时任务\n"
                "0 3 * * * /usr/bin/python3 /usr/local/bin/update-singbox-sub.py\n"
                "0 4 * * 0 /usr/local/bin/backup.sh >> /var/log/backup.log 2>&1\n"
                "*/5 * * * * /usr/local/bin/healthcheck.sh\n"
                "0 0 * * * /usr/bin/certbot renew --quiet\n"
                "30 2 * * * /usr/bin/apt-get update -qq\n\n"
                "## 9. 日志摘要 (最近24小时)\n"
                "### 9.1 系统日志\n"
                "syslog: 12,345 条 (其中 ERROR: 3, WARNING: 27)\n"
                "kern.log: 892 条 (无异常)\n"
                "auth.log: 1,847 条 (847 次失败登录)\n\n"
                "### 9.2 应用日志\n"
                "nginx access.log: 45,678 条请求\n"
                "  200: 42,312 (92.6%)\n"
                "  301: 1,234 (2.7%)\n"
                "  404: 1,567 (3.4%)\n"
                "  500: 23 (0.05%)\n"
                "  502: 542 (1.2%)\n"
                "postgresql: 2,341 条 (慢查询: 7 条, >1s)\n"
                "telegram_bot: 456 条 (ERROR: 0, WARNING: 2)\n\n"
                "## 10. 最近系统事件日志 (详细)\n"
                + "".join(
                    f"[{i:04d}] Apr 02 {10+i//60:02d}:{i%60:02d}:00 prod-server-01 "
                    f"{'sshd' if i%5==0 else 'nginx' if i%5==1 else 'postgresql' if i%5==2 else 'sing-box' if i%5==3 else 'telegram_bot'}"
                    f"[{10000+i}]: "
                    f"{'Connection from 45.148.10.' + str(i%256) + ' port ' + str(40000+i) + ' - Failed password for invalid user admin' if i%5==0 else ''}"
                    f"{'GET /api/v1/status HTTP/1.1 200 ' + str(100+i*3) + 'B ' + str(5+i%20) + 'ms - 192.168.1.' + str(i%50+10) if i%5==1 else ''}"
                    f"{'LOG:  duration: ' + str(50+i*7) + '.' + str(i%100) + ' ms  statement: SELECT * FROM events WHERE created_at > now() - interval ' + repr(str(i) + ' hours') + ' ORDER BY id DESC LIMIT 100' if i%5==2 else ''}"
                    f"{'[INFO] router: matched rule geosite-cn for domain cdn' + str(i) + '.example.com -> direct' if i%5==3 else ''}"
                    f"{'INFO handlers: Claude raw response (stop=end_turn): 这是第' + str(i) + '条测试消息的响应内容...' if i%5==4 else ''}"
                    "\n"
                    for i in range(200)
                ) +
                "\n## 11. 总结与建议\n"
                "1. 系统整体健康，负载较低，资源充足\n"
                "2. /data 分区使用率 63%，建议关注增长趋势\n"
                "3. 502 错误率略高 (1.2%)，建议检查 nginx upstream 配置\n"
                "4. 7 条慢查询需要优化，建议添加索引或优化 SQL\n"
                "5. SSH 暴力破解尝试频繁，fail2ban 运行正常，建议考虑改用密钥认证\n"
                "6. 所有关键服务运行正常，无需立即干预\n"
            ),
            "usage": {"input_tokens": 5200, "output_tokens": 3800},
            "tool_calls": []}},
        {"ts": t - 5, "type": "telegram_in", "data": {
            "message": {"chat": {"id": 12345}, "from": {"first_name": "Alice"},
                        "text": "!uptime"}}},
        {"ts": t - 4.9, "type": "route", "data": {
            "handler": "shell", "reason": "! prefix", "text": "!uptime"}},
        {"ts": t - 4, "type": "shell_exec", "data": {
            "command": "uptime", "output": " 14:23:07 up 42 days,  3:17,  2 users,  load average: 0.15, 0.22, 0.18",
            "exit_code": 0, "handler": "cmd"}},
        {"ts": t - 3, "type": "telegram_out", "data": {
            "method": "sendMessage",
            "payload": {"text": " 14:23:07 up 42 days,  3:17,  2 users,  load average: 0.15, 0.22, 0.18"},
            "status": 200}},
    ]


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
    parser.add_argument("--demo", action="store_true",
                        help="Load demo data without connecting to bot (for testing UI)")
    args = parser.parse_args()

    if args.demo:
        # Demo mode: no socket needed, inject sample events directly
        print("Demo mode: loading sample events...", file=sys.stderr)
        demo_events = _generate_demo_events()
        # For --live, pass a dummy socket (won't be used for reading)
        # We patch run_live to pre-load events
        if args.live:
            _run_live_demo(demo_events, args.filter_key, full=args.full)
        else:
            # Stream or raw: just print the events
            for ev in demo_events:
                etype = ev.get("type", "")
                if not _matches_filter(etype, args.filter_key):
                    continue
                if args.raw:
                    print(json.dumps(ev, ensure_ascii=False), flush=True)
                else:
                    ts = _ts(ev.get("ts", 0))
                    label = _label(etype)
                    formatter = _FORMATTERS.get(etype)
                    details = formatter(ev.get("data", {})) if formatter else json.dumps(ev.get("data", {}), ensure_ascii=False)[:200]
                    color = _ANSI.get(etype, "")
                    print(f"{_DIM}{ts}{_RESET}  {color}{label}{_RESET}  {details}", flush=True)
        return

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


def _run_live_demo(demo_events: list[dict], filter_key: str | None, full: bool = False):
    """Run --live TUI with pre-loaded demo events, no socket needed."""
    import os, select, shutil, termios, tty

    console = Console(highlight=False)

    # Reuse run_live internals by creating a dummy socketpair
    # The read end gets the demo events as JSON Lines, then EOF
    rsock, wsock = socket.socketpair()
    # Write all demo events
    for ev in demo_events:
        wsock.sendall((json.dumps(ev, ensure_ascii=False) + "\n").encode())
    wsock.close()  # EOF after demo data

    try:
        run_live(rsock, filter_key, full=full)
    finally:
        rsock.close()


if __name__ == "__main__":
    main()
