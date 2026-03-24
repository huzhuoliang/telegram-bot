"""Message handlers: shell execution, Claude AI, and preset responses."""

import json
import logging
import os
import re
import subprocess
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

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


def _run_cmd(cmd: str) -> str:
    """Execute a whitelisted command and return its output."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=_CMD_TIMEOUT
        )
        output = (result.stdout + result.stderr).strip()
        if len(output) > _CMD_MAX_OUTPUT:
            output = output[:_CMD_MAX_OUTPUT] + f"\n…(truncated)"
        return output or "(无输出)"
    except subprocess.TimeoutExpired:
        return f"(命令超时，超过 {_CMD_TIMEOUT} 秒)"
    except Exception as e:
        return f"(执行错误: {e})"


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
        return f"exit {result.returncode}\n{output}" if output else f"exit {result.returncode} (no output)"
    except subprocess.TimeoutExpired:
        return f"(timeout after {timeout}s)"
    except Exception as e:
        return f"(error: {e})"


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
            col_widths = [max(len(r[c]) for r in rows) for c in range(num_cols)]
            # Render
            formatted = []
            for j, row in enumerate(rows):
                formatted.append('  '.join(cell.ljust(col_widths[ci]) for ci, cell in enumerate(row)).rstrip())
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
- <pre>code block</pre> for multi-line code, shell output, config files
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

def _build_system_prompt(allowed_commands: list[str], backend: str = "cli") -> str:
    latex_section = _SYSTEM_PROMPT_LATEX_API if backend == "api" else _SYSTEM_PROMPT_LATEX_CLI
    return _SYSTEM_PROMPT_BASE + latex_section


class ShellHandler:
    def __init__(self, timeout: int = 30, max_chars: int = 3000, cwd: str = None):
        self.timeout = timeout
        self.max_chars = max_chars
        self.cwd = cwd or str(Path.home())

    def _is_sudo(self, command: str) -> bool:
        import shlex
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()
        return bool(tokens) and tokens[0] == "sudo"

    def handle(self, command: str) -> str:
        if not command:
            return "用法：!<shell 命令>"
        if self._is_sudo(command):
            return "错误：不允许使用 sudo"
        logger.info("Shell: %s", command)
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=self.cwd,
            )
            output = result.stdout + result.stderr
            truncated = False
            if len(output) > self.max_chars:
                output = output[: self.max_chars]
                truncated = True
            reply = f"Exit: {result.returncode}\n{output}"
            if truncated:
                reply += f"\n[truncated at {self.max_chars} chars]"
            return reply.strip()
        except subprocess.TimeoutExpired:
            return f"命令执行超时（{self.timeout} 秒）"
        except Exception as e:
            return f"错误：{e}"


class ClaudeHandler:
    """Supports two backends, switchable via the 'claude_backend' config key:

    - "cli"  (default): invokes `claude -p <text>` as a subprocess.
              Uses existing Claude Code credentials; no API key needed.
              Each call is stateless (no conversation history).

    - "api": calls Anthropic SDK directly.
              Requires ANTHROPIC_API_KEY env var.
              Maintains a rolling conversation history (claude_history_turns).

    Both backends support action markers ([PHOTO: url], [VIDEO: url]) in
    Claude's response, which are automatically extracted and sent via Telegram.
    """

    def __init__(
        self,
        backend: str = "cli",
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 1024,
        history_turns: int = 6,
        cli_timeout: int = 60,
        telegram_client=None,
        allowed_commands: list[str] = None,
    ):
        self.backend = backend
        self.model = model
        self.max_tokens = max_tokens
        self.history_turns = history_turns
        self.cli_timeout = cli_timeout
        self._telegram_client = telegram_client
        self._allowed_commands: list[str] = allowed_commands or []
        self._system_prompt = _build_system_prompt(self._allowed_commands, backend)
        self._history: list[dict] = []
        self._lock = threading.Lock()
        self._api_client = None  # lazy-init only if backend == "api"

    def _get_api_client(self):
        if self._api_client is None:
            import anthropic
            self._api_client = anthropic.Anthropic()
        return self._api_client

    def _execute_actions(self, response: str) -> str:
        """Extract [PHOTO:] / [VIDEO:] / [LATEX:] markers, execute them, return cleaned text."""
        if not self._telegram_client:
            return response

        # Handle LATEX markers (both [LATEX:...] and ```latex...``` code fences)
        def _run_latex(match):
            source = match.group(1).strip()
            # If source is just the body without \documentclass, wrap it
            if not source.lstrip().startswith('\\documentclass'):
                source = (
                    "\\documentclass[12pt]{article}\n"
                    "\\usepackage{amsmath,amssymb,geometry}\n"
                    "\\usepackage{xeCJK}\n"
                    "\\geometry{margin=2cm}\n"
                    "\\begin{document}\n"
                    + source +
                    "\n\\end{document}"
                )
            logger.info("Rendering LaTeX (%d chars)", len(source))
            png_pages, err = _render_latex(source)
            if err:
                logger.warning("LaTeX render error: %s", err[:200])
                self._telegram_client.send_message(f"⚠️ LaTeX 渲染失败：{err}", parse_mode="HTML")
            else:
                for p in png_pages:
                    self._telegram_client.send_photo(p)
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
            return ""

        response = _LATEX_RE.sub(_run_latex, response)
        response = _LATEX_FENCE_RE.sub(_run_latex, response)
        # Fallback: detect any raw \documentclass...\end{document} in the response
        response = _LATEX_DOC_RE.sub(_run_latex, response)

        def _run(match):
            kind = match.group(1)
            target = match.group(2).strip()
            caption = (match.group(3) or "").strip()
            logger.info("Action from Claude: %s %s", kind, target)
            if kind == "PHOTO":
                ok = self._telegram_client.send_photo(target, caption)
                if not ok:
                    self._telegram_client.send_message(f"⚠️ 图片发送失败：{target[:100]}")
            elif kind == "VIDEO":
                ok = self._telegram_client.send_video(target, caption)
                if not ok:
                    self._telegram_client.send_message(f"⚠️ 视频发送失败：{target[:100]}")
            return ""  # remove marker from text reply

        cleaned = _ACTION_RE.sub(_run, response).strip()
        # If the entire response was action markers, ensure we still send something
        return cleaned or "已发送。"

    def _extract_and_run_cmds(self, text: str) -> tuple[str, list[tuple[str, str]]]:
        """Extract [CMD:] markers, run whitelisted ones.
        Returns (text_without_cmd_markers, [(cmd, output), ...])."""
        results = []

        def _run(match):
            cmd = match.group(1).strip()
            exe = _cmd_executable(cmd)
            if exe not in self._allowed_commands:
                logger.warning("Blocked CMD (not in whitelist): %s", cmd)
                results.append((cmd, f"(拒绝执行：{exe} 不在白名单中)"))
                return ""
            logger.info("Claude CMD: %s", cmd)
            output = _run_cmd(cmd)
            results.append((cmd, output))
            return ""

        cleaned = _CMD_RE.sub(_run, text).strip()
        return cleaned, results

    def _call_cli(self, text: str) -> str:
        # CLI backend: single-shot call via `claude -p`.
        # Do NOT inject the TOOLS section — Claude Code CLI has its own tool
        # execution system that would intercept [CMD:] markers and show
        # permission prompts instead of outputting the markers as text.
        cli_prompt = _build_system_prompt([], "cli")  # no tools for cli backend
        prompt = f"{cli_prompt}\nUser: {text}"
        for _ in range(3):  # max 3 tool-call rounds
            result = subprocess.run(
                ["claude", "-p", prompt],
                capture_output=True, text=True, timeout=self.cli_timeout,
            )
            raw = result.stdout.strip() or result.stderr.strip() or "（无响应）"
            logger.info("Claude raw response: %s", raw[:300])
            _, cmd_results = self._extract_and_run_cmds(raw)
            if not cmd_results:
                break
            # Append tool outputs and re-prompt
            tool_block = "\n".join(f"$ {cmd}\n{out}" for cmd, out in cmd_results)
            prompt += f"\n\nAssistant: {raw}\n\nUser (tool results):\n{tool_block}\n\nPlease give your final answer based on the above."
        return self._execute_actions(_convert_md_tables(raw))

    def _build_tools(self) -> list[dict]:
        """Build the Anthropic tools list based on configuration."""
        tools = [
            {
                "name": "write_latex",
                "description": (
                    "Write (or append) text to a LaTeX source file on the server. "
                    "Use this to build large LaTeX documents in multiple chunks that would "
                    "exceed token limits if passed inline. Call with mode='write' first to "
                    "create/overwrite the file, then mode='append' for each subsequent chunk. "
                    "When done, call render_latex with the same path."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Absolute file path, e.g. /tmp/tgbot_novel.tex",
                        },
                        "content": {
                            "type": "string",
                            "description": "The LaTeX text to write or append.",
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["write", "append"],
                            "description": "'write' creates/overwrites; 'append' adds to existing file.",
                        },
                    },
                    "required": ["path", "content", "mode"],
                },
            },
            {
                "name": "render_latex",
                "description": (
                    "Compile a LaTeX document and send the rendered image(s) to the user. "
                    "For short documents, pass the full source in 'source'. "
                    "For long documents, first build the file with write_latex, then pass the file path in 'path'."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "source": {
                            "type": "string",
                            "description": (
                                "Inline LaTeX source (\\documentclass … \\end{document}). "
                                "Use XeLaTeX packages. For Chinese add \\usepackage{xeCJK}."
                            ),
                        },
                        "path": {
                            "type": "string",
                            "description": "Path to an existing .tex file written via write_latex.",
                        },
                    },
                },
            },
        ]
        if self._allowed_commands:
            tools.append({
                "name": "run_command",
                "description": (
                    "Run a whitelisted shell command on the server to fetch real-time information. "
                    f"Allowed executables: {', '.join(self._allowed_commands)}."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The shell command to execute.",
                        }
                    },
                    "required": ["command"],
                },
            })
        return tools

    def _handle_tool_call(self, tool_name: str, tool_input: dict) -> str:
        """Execute a tool call and return the result string."""
        if tool_name == "write_latex":
            path = tool_input.get("path", "").strip()
            content = tool_input.get("content", "")
            mode = tool_input.get("mode", "write")
            if not path:
                return "Error: path is required"
            try:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "w" if mode == "write" else "a", encoding="utf-8") as f:
                    f.write(content)
                size = os.path.getsize(path)
                logger.info("write_latex %s %s (%d bytes total)", mode, path, size)
                return f"OK: {size} bytes written to {path}"
            except Exception as e:
                return f"Error: {e}"

        if tool_name == "render_latex":
            # Accept either inline source or a file path
            path = tool_input.get("path", "").strip()
            if path:
                try:
                    with open(path, encoding="utf-8") as f:
                        source = f.read()
                except Exception as e:
                    return f"Error reading {path}: {e}"
            else:
                source = tool_input.get("source", "")
            if not source.lstrip().startswith('\\documentclass'):
                source = (
                    "\\documentclass[12pt]{article}\n"
                    "\\usepackage{amsmath,amssymb,geometry}\n"
                    "\\usepackage{xeCJK}\n"
                    "\\geometry{margin=2cm}\n"
                    "\\begin{document}\n"
                    + source +
                    "\n\\end{document}"
                )
            logger.info("Tool: render_latex (%d chars)", len(source))
            png_pages, err = _render_latex(source)
            if err:
                logger.warning("LaTeX render error: %s", err[:200])
                if self._telegram_client:
                    self._telegram_client.send_message(f"⚠️ LaTeX 渲染失败：{err}", parse_mode="HTML")
                return f"Error: {err}"
            if self._telegram_client:
                for p in png_pages:
                    self._telegram_client.send_photo(p)
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
            return f"{len(png_pages)} page(s) sent successfully."

        if tool_name == "run_command":
            cmd = tool_input.get("command", "")
            exe = _cmd_executable(cmd)
            if exe not in self._allowed_commands:
                return f"Error: '{exe}' is not in the allowed commands list."
            logger.info("Tool: run_command: %s", cmd)
            return _run_cmd(cmd)

        return f"Error: unknown tool '{tool_name}'"

    @staticmethod
    def _is_text_user_message(msg: dict) -> bool:
        """Return True if msg is a user message with text (not tool_results)."""
        if msg.get("role") != "user":
            return False
        content = msg.get("content")
        if isinstance(content, str):
            return True
        if isinstance(content, list):
            return any(
                isinstance(b, dict) and b.get("type") != "tool_result"
                for b in content
            )
        return True

    @staticmethod
    def _block_has_tool_use(content) -> bool:
        if not isinstance(content, list):
            return False
        for b in content:
            btype = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
            if btype == "tool_use":
                return True
        return False

    def _sanitize_history(self):
        """Remove any trailing assistant(tool_use) without a following tool_result.
        This can happen if a previous call crashed mid-round."""
        while (
            len(self._history) >= 1
            and self._history[-1].get("role") == "assistant"
            and self._block_has_tool_use(self._history[-1].get("content", []))
        ):
            logger.warning("Removing orphaned tool_use from history tail")
            self._history.pop()

    def _call_api(self, text: str, image_data: bytes = None) -> str:
        # history is already locked by caller.
        # Save state so we can roll back on any error.
        import base64 as _b64
        self._sanitize_history()
        saved_history = list(self._history)
        try:
            if image_data:
                content = [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": _b64.b64encode(image_data).decode(),
                        },
                    },
                    {"type": "text", "text": text},
                ]
            else:
                content = text
            self._history.append({"role": "user", "content": content})
            max_msgs = self.history_turns * 2
            if len(self._history) > max_msgs:
                trimmed = self._history[-max_msgs:]
                # Advance past any leading tool_result / assistant messages so
                # we never send an orphaned tool_use block to the API.
                start = 0
                while start < len(trimmed) and not self._is_text_user_message(trimmed[start]):
                    start += 1
                self._history = trimmed[start:]

            client = self._get_api_client()
            tools = self._build_tools()

            for _ in range(5):  # max 5 tool-call rounds
                response = client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=self._system_prompt,
                    messages=self._history,
                    tools=tools,
                )

                # Collect text and tool_use blocks
                text_parts = []
                tool_calls = []
                for block in response.content:
                    if block.type == "text":
                        text_parts.append(block.text)
                    elif block.type == "tool_use":
                        tool_calls.append(block)

                if response.stop_reason != "tool_use" or not tool_calls:
                    # Final text response
                    raw = "\n".join(text_parts).strip()
                    logger.info("Claude raw response (stop=%s): %s", response.stop_reason, raw[:500])
                    cleaned = self._execute_actions(_convert_md_tables(raw))
                    self._history.append({"role": "assistant", "content": response.content})
                    return cleaned or "已完成。"

                # Execute tool calls and feed results back
                self._history.append({"role": "assistant", "content": response.content})
                tool_results = []
                for tc in tool_calls:
                    result = self._handle_tool_call(tc.name, tc.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "content": result,
                    })
                self._history.append({"role": "user", "content": tool_results})

            # Exhausted rounds — response.content (tool_use) was already appended
            # inside the loop; do NOT append again to avoid orphaned tool_use blocks.
            raw = "\n".join(
                block.text for block in response.content if block.type == "text"
            ).strip()
            cleaned = self._execute_actions(_convert_md_tables(raw))
            return cleaned or "已完成。"

        except Exception:
            self._history = saved_history  # roll back all history changes
            raise

    def handle(self, text: str) -> str:
        if not text:
            return "用法：?<问题>"
        logger.info("Claude [%s]: %s", self.backend, text[:80])
        processing_msg_id = None
        if self._telegram_client:
            processing_msg_id = self._telegram_client.send_message("⏳ 处理中...")
        with self._lock:
            try:
                if self.backend == "api":
                    reply = self._call_api(text)
                else:
                    reply = self._call_cli(text)
            except subprocess.TimeoutExpired:
                reply = f"Claude 响应超时（{self.cli_timeout} 秒）"
            except FileNotFoundError:
                reply = "错误：PATH 中未找到 claude 命令"
            except Exception as e:
                # _call_api rolls back history on error; nothing to do here.
                logger.warning("Claude error: %s", e)
                reply = f"Claude 错误：{e}"
        if processing_msg_id and self._telegram_client:
            self._telegram_client.delete_message(processing_msg_id)
        if self._telegram_client:
            self._telegram_client.send_message(reply, parse_mode="HTML")
            return None
        return reply

    def handle_with_image(self, text: str, file_id: str) -> str:
        """Download a Telegram photo and send it to Claude for recognition (api backend only)."""
        if self.backend != "api":
            return "⚠️ 识图仅支持 api backend。"
        import tempfile
        logger.info("Claude image [%s]: %s", self.backend, text[:80])
        processing_msg_id = None
        if self._telegram_client:
            processing_msg_id = self._telegram_client.send_message("⏳ 处理中...")
        tmp_path = None
        image_data = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp_path = tmp.name
            ok = self._telegram_client.download_file(file_id, tmp_path)
            if ok:
                with open(tmp_path, "rb") as f:
                    image_data = f.read()
        except Exception as e:
            logger.warning("Image download failed: %s", e)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
        with self._lock:
            try:
                reply = self._call_api(text, image_data=image_data)
            except Exception as e:
                logger.warning("Claude image error: %s", e)
                reply = f"Claude 错误：{e}"
        if processing_msg_id and self._telegram_client:
            self._telegram_client.delete_message(processing_msg_id)
        if self._telegram_client:
            self._telegram_client.send_message(reply, parse_mode="HTML")
            return None
        return reply

    def help(self) -> str:
        with self._lock:
            backend = self.backend
            allowed = list(self._allowed_commands)
        help_file = Path(__file__).parent / "help.txt"
        static = help_file.read_text(encoding="utf-8").rstrip()
        lines = [static, f"\n<b>当前 Backend：</b><code>{backend}</code>"]
        if backend == "api":
            lines.append("  ✅ 支持多轮对话历史")
            if allowed:
                lines.append(f"  ✅ 可用工具命令：{', '.join(f'<code>{c}</code>' for c in allowed)}")
                lines.append("     直接提问，Claude 会按需自动调用")
        else:
            lines.append("  ℹ️ 无对话历史（每次独立）")
            if allowed:
                lines.append(f"  ℹ️ 工具命令（{', '.join(allowed)}）需切换到 api backend 后生效")
        return self._send_html("\n".join(lines))

    def _send_html(self, text: str):
        """Send HTML-formatted text via telegram client; return text if no client."""
        if self._telegram_client:
            self._telegram_client.send_message(text, parse_mode="HTML")
            return None
        return text

    def clear_history(self) -> str:
        with self._lock:
            self._history.clear()
        return self._send_html("对话历史已清除。")

    def _update_config_backend(self, backend: str, config_path: str) -> bool:
        """Write claude_backend value to config.json. Returns True on success."""
        try:
            with open(config_path) as f:
                cfg = json.load(f)
            cfg["claude_backend"] = backend
            with open(config_path, "w") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
                f.write("\n")
            return True
        except Exception as e:
            logger.warning("Failed to update config: %s", e)
            return False

    def configure_api_backend(self, api_key: str, config_path: str = None) -> str:
        """Validate ANTHROPIC_API_KEY, switch to api backend, persist both to disk."""
        api_key = api_key.strip()

        # Validate key before saving
        try:
            import anthropic
            test_client = anthropic.Anthropic(api_key=api_key)
            test_client.messages.create(
                model=self.model,
                max_tokens=8,
                messages=[{"role": "user", "content": "hi"}],
            )
        except Exception as e:
            return self._send_html(f"❌ API key 验证失败：<code>{e}</code>")

        os.environ["ANTHROPIC_API_KEY"] = api_key

        # Persist key to api_key.txt next to this file
        key_file = Path(__file__).parent / "api_key.txt"
        key_file.write_text(api_key + "\n")
        key_file.chmod(0o600)

        # Switch backend and reset client + history
        with self._lock:
            self.backend = "api"
            self._system_prompt = _build_system_prompt(self._allowed_commands, "api")
            self._api_client = None
            self._history.clear()

        if config_path and not self._update_config_backend("api", config_path):
            return self._send_html("⚠️ API key 验证通过并已生效，但写入 config.json 失败，重启后需重新设置。")

        return self._send_html("✅ API key 验证通过，已切换到 <code>api</code> backend，对话历史已清除。")

    def status(self) -> str:
        with self._lock:
            backend = self.backend
            history_len = len(self._history) // 2  # turns
        if backend == "api":
            key = os.environ.get("ANTHROPIC_API_KEY", "")
            key_display = f"{key[:8]}...{key[-4:]}" if len(key) > 12 else "(未设置)"
            text = (
                f"Backend: <b>api</b>（Anthropic SDK）\n"
                f"Model: <code>{self.model}</code>\n"
                f"API key: <code>{key_display}</code>\n"
                f"对话历史：{history_len} 轮（最多 {self.history_turns} 轮）"
            )
        else:
            text = (
                f"Backend: <b>cli</b>（<code>claude -p</code>）\n"
                f"对话历史：无（cli backend 无状态）"
            )
        return self._send_html(text)

    def configure_cli_backend(self, config_path: str = None) -> str:
        """Switch back to cli backend and persist the change."""
        with self._lock:
            self.backend = "cli"
            self._system_prompt = _build_system_prompt(self._allowed_commands, "cli")
            self._history.clear()

        # Remove api_key.txt so it doesn't auto-load on restart
        key_file = Path(__file__).parent / "api_key.txt"
        if key_file.exists():
            key_file.unlink()

        if config_path and not self._update_config_backend("cli", config_path):
            return self._send_html("⚠️ 已切换到 <code>cli</code> backend，但写入 config.json 失败，重启后需重新设置。")

        return self._send_html("✅ 已切换到 <code>cli</code> backend（<code>claude -p</code>），对话历史已清除。")


class PresetHandler:
    def __init__(self, presets: dict):
        self._presets = {k.lower(): v for k, v in presets.items()}

    def handle(self, text: str) -> str | None:
        return self._presets.get(text.lower().strip())


class MediaArchiveHandler:
    """Saves incoming photos and videos forwarded to the bot."""

    _index_lock = threading.Lock()

    def __init__(self, archive_dir: str, telegram_client):
        self.archive_dir = Path(archive_dir).expanduser()
        self._client = telegram_client

    def _append_index(self, file_id: str, media_type: str, rel_path: str, ts: str):
        index_path = self.archive_dir / "archive_index.json"
        tmp_path = self.archive_dir / "archive_index.json.tmp"
        with MediaArchiveHandler._index_lock:
            try:
                entries = json.loads(index_path.read_text()).get("entries", []) if index_path.exists() else []
            except Exception:
                entries = []
            entries.append({"type": media_type, "file_id": file_id, "rel_path": rel_path, "ts": ts})
            tmp_path.write_text(json.dumps({"entries": entries}))
            os.replace(tmp_path, index_path)

    def handle(self, message: dict) -> str:
        import datetime
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        ts_readable = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if "photo" in message:
            # Telegram sends multiple sizes; pick the largest (last in array)
            file_id = message["photo"][-1]["file_id"]
            save_path = str(self.archive_dir / "photos" / f"{ts}.jpg")
            kind = "图片"
            kind_key = "photo"
        elif "video" in message:
            file_id = message["video"]["file_id"]
            ext = message["video"].get("mime_type", "video/mp4").split("/")[-1]
            save_path = str(self.archive_dir / "videos" / f"{ts}.{ext}")
            kind = "视频"
            kind_key = "video"
        elif "document" in message:
            doc = message["document"]
            file_id = doc["file_id"]
            filename = doc.get("file_name", f"{ts}.bin")
            save_path = str(self.archive_dir / "documents" / filename)
            kind = "文件"
            kind_key = "document"
        else:
            return "不支持的媒体类型。"

        logger.info("Archiving %s → %s", kind, save_path)
        ok = self._client.download_file(file_id, save_path)
        if ok:
            rel = str(Path(save_path).relative_to(self.archive_dir))
            self._append_index(file_id, kind_key, rel, ts_readable)
            return f"✅ {kind}已存档：{save_path}"
        else:
            return f"❌ {kind}存档失败，请查看日志。"


class FileArchiveHandler:
    """Browse, preview and download archived media files via inline keyboard."""

    PAGE_SIZE = 8
    TYPE_LABELS = {"photo": "📷 照片", "video": "📹 视频", "document": "📄 文档"}

    def __init__(self, archive_dir: str, telegram_client):
        self.archive_dir = Path(archive_dir).expanduser()
        self._client = telegram_client

    def _load_index(self) -> list:
        path = self.archive_dir / "archive_index.json"
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text()).get("entries", [])
        except Exception:
            return []

    def _main_menu_markup(self, entries: list) -> dict:
        counts = {t: sum(1 for e in entries if e["type"] == t) for t in ("photo", "video", "document")}
        buttons = [
            {"text": f"📷 照片 ({counts['photo']})", "callback_data": "files:photo:0"},
            {"text": f"📹 视频 ({counts['video']})", "callback_data": "files:video:0"},
            {"text": f"📄 文档 ({counts['document']})", "callback_data": "files:document:0"},
        ]
        return {"inline_keyboard": [buttons]}

    def handle_command(self):
        """Handle /files command — sends the main menu."""
        entries = self._load_index()
        markup = self._main_menu_markup(entries)
        self._client.send_message_with_keyboard("📁 归档文件", markup)

    def handle_callback(self, callback_query: dict):
        """Handle all files:* and file:* callback queries."""
        cq_id = callback_query["id"]
        data = callback_query.get("data", "")
        message_id = callback_query.get("message", {}).get("message_id")

        # Answer immediately to dismiss the loading spinner
        self._client.answer_callback_query(cq_id)

        parts = data.split(":", 2)
        if not parts:
            return

        if parts[0] == "files":
            if len(parts) == 2 and parts[1] == "menu":
                entries = self._load_index()
                markup = self._main_menu_markup(entries)
                self._client.edit_message_keyboard(message_id, "📁 归档文件", markup)
            elif len(parts) == 3:
                media_type = parts[1]
                try:
                    page = int(parts[2])
                except ValueError:
                    return
                self._show_page(message_id, media_type, page)

        elif parts[0] == "file" and len(parts) == 3:
            media_type = parts[1]
            try:
                idx = int(parts[2])
            except ValueError:
                return
            entries = self._load_index()
            if 0 <= idx < len(entries) and entries[idx]["type"] == media_type:
                entry = entries[idx]
                ok = self._client.send_by_file_id(media_type, entry["file_id"])
                if not ok:
                    abs_path = str(self.archive_dir / entry["rel_path"])
                    if media_type == "photo":
                        self._client.send_photo(abs_path)
                    elif media_type == "video":
                        self._client.send_video(abs_path)

    def _show_page(self, message_id: int, media_type: str, page: int):
        entries = self._load_index()
        typed = [(i, e) for i, e in enumerate(entries) if e["type"] == media_type]
        typed.reverse()  # newest first
        total = len(typed)
        label = self.TYPE_LABELS.get(media_type, media_type)

        if total == 0:
            markup = {"inline_keyboard": [[{"text": "🔙 返回", "callback_data": "files:menu"}]]}
            self._client.edit_message_keyboard(message_id, f"{label}\n暂无文件", markup)
            return

        total_pages = (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE
        page = max(0, min(page, total_pages - 1))
        start = page * self.PAGE_SIZE
        chunk = typed[start:start + self.PAGE_SIZE]

        rows = []
        for orig_idx, entry in chunk:
            rows.append([{"text": entry["ts"], "callback_data": f"file:{media_type}:{orig_idx}"}])

        nav = []
        if page > 0:
            nav.append({"text": "◀ 上一页", "callback_data": f"files:{media_type}:{page - 1}"})
        if page < total_pages - 1:
            nav.append({"text": "下一页 ▶", "callback_data": f"files:{media_type}:{page + 1}"})
        nav.append({"text": "🔙 返回", "callback_data": "files:menu"})
        rows.append(nav)

        text = f"{label} ({total})  第 {page + 1}/{total_pages} 页"
        markup = {"inline_keyboard": rows}
        self._client.edit_message_keyboard(message_id, text, markup)


# ---------------------------------------------------------------------------
# Privileged Claude handler
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_PRIVILEGED = """You are a privileged system administration AI running as a Telegram bot on the user's personal Linux server. The user has full administrative trust.

LANGUAGE: Always respond in Chinese (中文), unless the content is code, shell output, file paths, or technical strings.

FORMATTING: Responses are sent via Telegram with parse_mode=HTML. Use HTML tags:
- <b>bold</b> for section headings and important notes
- <code>inline code</code> for commands, file paths, variable names
- <pre>block</pre> for shell output, file contents, multi-line code
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
"""


class PrivilegedClaudeHandler(ClaudeHandler):
    """Unrestricted Claude AI with full shell, file-read, and file-write access.

    Triggered by the '#' prefix in router.py. Always uses the 'api' backend.
    Maintains a separate conversation history from the regular ClaudeHandler.
    """

    def __init__(
        self,
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 4096,
        history_turns: int = 6,
        shell_timeout: int = 60,
        telegram_client=None,
    ):
        super().__init__(
            backend="api",
            model=model,
            max_tokens=max_tokens,
            history_turns=history_turns,
            cli_timeout=120,
            telegram_client=telegram_client,
            allowed_commands=[],
        )
        self._shell_timeout = shell_timeout
        self._system_prompt = _SYSTEM_PROMPT_PRIVILEGED

    def _build_tools(self) -> list[dict]:
        return [
            {
                "name": "run_shell_command",
                "description": (
                    "Run any shell command on the server, including sudo commands. "
                    "Returns exit code + combined stdout/stderr."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "The shell command to execute."}
                    },
                    "required": ["command"],
                },
            },
            {
                "name": "read_file",
                "description": "Read the full contents of any file by absolute path.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute file path to read."}
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "write_file",
                "description": (
                    "Write (overwrite) a file at the given absolute path with new content. "
                    "Creates parent directories if needed."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute file path to write."},
                        "content": {"type": "string", "description": "Full new content of the file."},
                    },
                    "required": ["path", "content"],
                },
            },
        ]

    def _handle_tool_call(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "run_shell_command":
            cmd = tool_input.get("command", "").strip()
            if not cmd:
                return "Error: command is empty"
            logger.info("Privileged CMD: %s", cmd)
            return _run_privileged_cmd(cmd, timeout=self._shell_timeout)

        if tool_name == "read_file":
            path = tool_input.get("path", "").strip()
            logger.info("Privileged read_file: %s", path)
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    content = f.read()
                if len(content) > 8000:
                    content = content[:8000] + "\n…(truncated at 8000 chars)"
                return content
            except Exception as e:
                return f"Error reading {path}: {e}"

        if tool_name == "write_file":
            path = tool_input.get("path", "").strip()
            content = tool_input.get("content", "")
            logger.info("Privileged write_file: %s (%d chars)", path, len(content))
            try:
                parent = os.path.dirname(path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                return f"OK: wrote {os.path.getsize(path)} bytes to {path}"
            except Exception as e:
                return f"Error writing {path}: {e}"

        return super()._handle_tool_call(tool_name, tool_input)

    def handle(self, text: str) -> str:
        if not text:
            return self._send_html("用法：<code>$&lt;指令&gt;</code>")
        return super().handle(text)

    def clear_history(self) -> str:
        with self._lock:
            self._history.clear()
        return self._send_html("特权对话历史已清除。")
