"""Shared constants, patterns, and utility functions for message handlers."""

import json
import logging
import os
import re
import subprocess
from pathlib import Path

import debug_bus

logger = logging.getLogger(__name__)

# Project root directory (one level up from handlers/)
BASE_DIR = Path(__file__).resolve().parent.parent

# Claude uses these markers in its response to trigger media actions.
# Formats:
#   [PHOTO: <url_or_path>]
#   [PHOTO: <url_or_path> | <caption>]
#   [VIDEO: <url_or_path>]
#   [VIDEO: <url_or_path> | <caption>]
_ACTION_RE = re.compile(r'\[(PHOTO|VIDEO):\s*([^\]|]+?)(?:\s*\|\s*([^\]]*))?\]')
_CMD_RE = re.compile(r'\[CMD:\s*([^\]]+?)\]')
_LATEX_RE = re.compile(r'\[LATEX:\s*\n?(.*?)\n?\s*\]', re.DOTALL)
_LATEX_FENCE_RE = re.compile(r'```latex\s*\n(.*?)\n```', re.DOTALL)
_LATEX_DOC_RE = re.compile(r'(\\documentclass[\s\S]*?\\end\{document\})')

_CMD_TIMEOUT = 15  # seconds per tool command
_CMD_MAX_OUTPUT = 2000  # chars

# Matches <pre> blocks that have no language tag — used by _ensure_pre_language.
_PRE_NO_LANG_RE = re.compile(r'<pre>(?!<code\s+class=)(.*?)</pre>', re.DOTALL)

# Splits HTML into alternating plain-text / already-protected-block segments.
# Odd-indexed segments are inside <code> or <pre> and must not be modified.
_CODE_SPLIT_RE = re.compile(r'(<(?:code|pre)[^>]*>.*?</(?:code|pre)>)', re.DOTALL)
# Absolute paths that Telegram would linkify as bot commands (e.g. /etc/nginx/nginx.conf).
# Lookbehind excludes HTML closing tags (preceded by <), attributes (= or "), and existing paths.
_ABS_PATH_RE = re.compile(r'(?<![="\'/\w<])/[a-zA-Z][/\w._-]{2,}')


def _ensure_pre_language(text: str) -> str:
    """Wrap bare <pre>…</pre> blocks with <code class="language-text"> so Telegram
    always shows a language label.  Blocks that already have a language tag are left
    untouched."""
    def _wrap(m):
        return f'<pre><code class="language-text">{m.group(1)}</code></pre>'
    return _PRE_NO_LANG_RE.sub(_wrap, text)


def _protect_file_paths(text: str) -> str:
    """Wrap bare absolute file paths (e.g. /etc/nginx/nginx.conf) in <code> tags so
    Telegram does not linkify them as bot commands.  Paths already inside <code>/<pre>
    blocks are left untouched."""
    parts = _CODE_SPLIT_RE.split(text)
    for i, part in enumerate(parts):
        if i % 2 == 0:  # plain-text segment
            parts[i] = _ABS_PATH_RE.sub(lambda m: f'<code>{m.group()}</code>', part)
    return ''.join(parts)


def _run_cmd(cmd: str) -> str:
    """Execute a whitelisted command and return its output."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=_CMD_TIMEOUT
        )
        output = (result.stdout + result.stderr).strip()
        if len(output) > _CMD_MAX_OUTPUT:
            output = output[:_CMD_MAX_OUTPUT] + f"\n…(truncated)"
        output = output or "(无输出)"
        debug_bus.emit("shell_exec", {"command": cmd, "output": output, "exit_code": result.returncode, "handler": "cmd"})
        return output
    except subprocess.TimeoutExpired:
        output = f"(命令超时，超过 {_CMD_TIMEOUT} 秒)"
        debug_bus.emit("shell_exec", {"command": cmd, "output": output, "exit_code": -1, "handler": "cmd"})
        return output
    except Exception as e:
        output = f"(执行错误: {e})"
        debug_bus.emit("shell_exec", {"command": cmd, "output": output, "exit_code": -1, "handler": "cmd"})
        return output


def _run_privileged_cmd(cmd: str, timeout: int = 60) -> str:
    """Execute any shell command (including sudo) without restrictions."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=str(Path.home()),
        )
        output = (result.stdout + result.stderr).strip()
        if len(output) > 4000:
            output = output[:4000] + "\n…(truncated)"
        full = f"exit {result.returncode}\n{output}" if output else f"exit {result.returncode} (no output)"
        debug_bus.emit("shell_exec", {"command": cmd, "output": full, "exit_code": result.returncode, "handler": "privileged"})
        return full
    except subprocess.TimeoutExpired:
        output = f"(timeout after {timeout}s)"
        debug_bus.emit("shell_exec", {"command": cmd, "output": output, "exit_code": -1, "handler": "privileged"})
        return output
    except Exception as e:
        output = f"(error: {e})"
        debug_bus.emit("shell_exec", {"command": cmd, "output": output, "exit_code": -1, "handler": "privileged"})
        return output


def _cmd_executable(cmd: str) -> str:
    """Return the executable name (first token) of a command string."""
    import shlex
    try:
        tokens = shlex.split(cmd.strip())
    except ValueError:
        tokens = cmd.strip().split()
    return tokens[0] if tokens else ""

_TABLE_ROW_RE = re.compile(r'^\s*\|')
_TABLE_SEP_RE = re.compile(r'^\s*\|[\s|:-]+\|\s*$')


def _str_display_width(s: str) -> int:
    """Return display width of s in a monospace font: CJK wide/fullwidth chars count as 2."""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in s)


def _str_ljust(s: str, width: int) -> str:
    """Left-justify s to display width using space padding."""
    return s + ' ' * max(0, width - _str_display_width(s))


def _convert_md_tables(text: str) -> str:
    """Convert Markdown pipe tables to <pre>-formatted aligned text for Telegram."""
    lines = text.split('\n')
    result = []
    i = 0
    while i < len(lines):
        if _TABLE_ROW_RE.match(lines[i]):
            # Collect contiguous table lines
            table_lines = []
            while i < len(lines) and _TABLE_ROW_RE.match(lines[i]):
                table_lines.append(lines[i])
                i += 1
            # Parse rows, skip separator lines
            rows = []
            for tl in table_lines:
                if _TABLE_SEP_RE.match(tl):
                    continue
                cells = [c.strip() for c in tl.strip().strip('|').split('|')]
                rows.append(cells)
            if not rows:
                continue
            # Normalize column count
            num_cols = max(len(r) for r in rows)
            rows = [r + [''] * (num_cols - len(r)) for r in rows]
            # Use display width (CJK = 2) so header/separator/data align correctly
            col_widths = [max(_str_display_width(r[c]) for r in rows) for c in range(num_cols)]
            # Render
            formatted = []
            for j, row in enumerate(rows):
                formatted.append('  '.join(_str_ljust(cell, col_widths[ci]) for ci, cell in enumerate(row)).rstrip())
                if j == 0:
                    formatted.append('  '.join('-' * w for w in col_widths))
            result.append('<pre>' + '\n'.join(formatted) + '</pre>')
        else:
            result.append(lines[i])
            i += 1
    return '\n'.join(result)

def _render_latex(source: str) -> tuple[list[str], str | None]:
    """Compile LaTeX source with xelatex, convert all pages to PNG.
    Returns ([png_paths], None) on success, ([], error_message) on failure.
    Caller is responsible for deleting the png files."""
    import tempfile, shutil, glob as _glob
    tmpdir = tempfile.mkdtemp(prefix="tgbot_latex_")
    try:
        tex_path = os.path.join(tmpdir, "doc.tex")
        pdf_path = os.path.join(tmpdir, "doc.pdf")
        with open(tex_path, "w", encoding="utf-8") as f:
            f.write(source)
        # Compile
        result = subprocess.run(
            ["xelatex", "-interaction=nonstopmode", "-output-directory", tmpdir, tex_path],
            capture_output=True, text=True, timeout=60,
        )
        if not os.path.exists(pdf_path):
            log_snippet = (result.stdout + result.stderr)[-800:]
            return [], f"LaTeX 编译失败：\n<pre>{log_snippet}</pre>"
        # Convert all pages to PNG (200 dpi); pdftoppm names them doc-1.png, doc-2.png, ...
        result2 = subprocess.run(
            ["pdftoppm", "-png", "-r", "200", pdf_path, os.path.join(tmpdir, "doc")],
            capture_output=True, text=True, timeout=60,
        )
        pages = sorted(_glob.glob(os.path.join(tmpdir, "doc-*.png")))
        if not pages:
            return [], f"PDF 转图片失败：{result2.stderr[:300]}"
        # Move PNGs out of tmpdir before cleanup
        import shutil as _shutil
        out_pages = []
        for p in pages:
            out = tempfile.mktemp(suffix=".png", prefix="tgbot_latex_")
            _shutil.move(p, out)
            out_pages.append(out)
        return out_pages, None
    except subprocess.TimeoutExpired:
        return [], "LaTeX 编译超时（>60 秒）"
    except FileNotFoundError as e:
        return [], f"缺少依赖：{e}（请确认已安装 xelatex 和 poppler-utils）"
    except Exception as e:
        return [], f"渲染错误：{e}"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


_SYSTEM_PROMPT_BASE = """You are a helpful assistant running as a Telegram bot on the user's personal Linux server. The user interacts with you through Telegram chat.

LANGUAGE: Always respond in Chinese (中文), unless the content is inherently non-Chinese (code, shell output, technical strings, proper nouns).

FORMATTING: Your response is sent via Telegram with parse_mode=HTML. Use HTML tags to format your replies clearly:
- <b>bold</b> for emphasis, headings, key terms
- <i>italic</i> for secondary emphasis
- <code>inline code</code> for commands, file paths, variable names, short code snippets
- <pre><code class="language-xxx">code block</code></pre> for multi-line code, shell output, config files — ALWAYS specify the language (bash, python, json, yaml, text, etc.); use "text" when unsure
- <u>underline</u> sparingly
- Do NOT use Markdown syntax (no **, __, `, ``` etc.) — use HTML only.
- Do NOT use Markdown pipe tables (|col|col|). For tabular data, use <pre> with space-aligned columns or a plain list.
- Do NOT wrap the entire response in a single code block.
- Keep responses concise and well-structured. Use line breaks between sections.

MEDIA: To send a photo or video, embed these markers anywhere in your response:
  [PHOTO: <url>]
  [PHOTO: <url> | <caption>]
  [VIDEO: <url>]
  [VIDEO: <url> | <caption>]
The markers are extracted and executed automatically. When asked for an image/video, use a real publicly accessible URL and include the marker — do NOT just describe what you would do.

RULES:
1. Every response MUST contain at least one sentence of plain text (not just markers).
2. Never tell the user to run a command themselves when you can answer directly.
3. Escape HTML special characters in user-supplied content if you echo it back: & → &amp;  < → &lt;  > → &gt;
"""

_SYSTEM_PROMPT_LATEX_API = """
LATEX (api backend): You have a render_latex tool. When the user asks for a LaTeX document or math formula, call render_latex IMMEDIATELY — do NOT write any acknowledgment or explanation text first. Just call the tool directly. After the tool returns, you may add a brief comment. Do NOT output LaTeX source as plain text.
"""

_SYSTEM_PROMPT_LATEX_CLI = """
LATEX (cli backend): To render a LaTeX document, embed the source in your response using this marker:
  [LATEX:
  <complete LaTeX source starting with \\documentclass>
  ]
Use this whenever the user asks to render math formulas or LaTeX documents. Do NOT output the source as plain text.
"""

_SYSTEM_PROMPT_PRIVILEGED = """You are a privileged system administration AI running as a Telegram bot on the user's personal Linux server. The user has full administrative trust.

LANGUAGE: Always respond in Chinese (中文), unless the content is code, shell output, file paths, or technical strings.

FORMATTING: Responses are sent via Telegram with parse_mode=HTML. Use HTML tags:
- <b>bold</b> for section headings and important notes
- <code>inline code</code> for commands, file paths, variable names
- <pre><code class="language-xxx">block</code></pre> for shell output, file contents, multi-line code — ALWAYS specify the language (bash, python, json, yaml, text, etc.); use "text" when unsure
- Do NOT use Markdown syntax (no ** __ ` ``` etc.)
- Keep responses concise — show what was done, then summarize.

TOOLS AVAILABLE:
- run_shell_command: Run any shell command, including sudo. You have full system access.
- read_file: Read any file by absolute path.
- write_file: Write or overwrite any file by absolute path.

OPERATING PRINCIPLES:
1. Execute tasks directly — do NOT ask "are you sure?" for routine operations.
2. For destructive operations (rm -rf, overwriting critical configs), briefly state what you are about to do before calling the tool, then proceed.
3. Always show the shell output or relevant file content so the user can verify.
4. If a command needs sudo and fails due to permissions, clearly say so and suggest the sudoers fix.
5. Every response must include at least one sentence of plain Chinese text.
6. Escape HTML in user-supplied content echoed back: & → &amp;  < → &lt;  > → &gt;
7. If a tool result starts with "REJECTED:", the user has explicitly refused this operation. Stop immediately, report the refusal in Chinese, and do NOT suggest alternatives or retry.
"""


def _build_system_prompt(allowed_commands: list[str], backend: str = "cli") -> str:
    latex_section = _SYSTEM_PROMPT_LATEX_API if backend == "api" else _SYSTEM_PROMPT_LATEX_CLI
    return _SYSTEM_PROMPT_BASE + latex_section
