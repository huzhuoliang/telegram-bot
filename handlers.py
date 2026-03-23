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

def _render_latex(source: str) -> tuple[str | None, str | None]:
    """Compile LaTeX source with xelatex, convert first page to PNG.
    Returns (png_path, None) on success, (None, error_message) on failure.
    Caller is responsible for deleting the png file."""
    import tempfile, shutil
    tmpdir = tempfile.mkdtemp(prefix="tgbot_latex_")
    try:
        tex_path = os.path.join(tmpdir, "doc.tex")
        pdf_path = os.path.join(tmpdir, "doc.pdf")
        png_path = os.path.join(tmpdir, "doc.png")
        with open(tex_path, "w", encoding="utf-8") as f:
            f.write(source)
        # Compile
        result = subprocess.run(
            ["xelatex", "-interaction=nonstopmode", "-output-directory", tmpdir, tex_path],
            capture_output=True, text=True, timeout=60,
        )
        if not os.path.exists(pdf_path):
            log_snippet = (result.stdout + result.stderr)[-800:]
            return None, f"LaTeX 编译失败：\n<pre>{log_snippet}</pre>"
        # Convert first page to PNG (300 dpi)
        result2 = subprocess.run(
            ["pdftoppm", "-png", "-r", "200", "-singlefile", pdf_path,
             os.path.join(tmpdir, "doc")],
            capture_output=True, text=True, timeout=30,
        )
        if not os.path.exists(png_path):
            return None, f"PDF 转图片失败：{result2.stderr[:300]}"
        # Move PNG out of tmpdir before cleanup
        import shutil as _shutil
        out_png = tempfile.mktemp(suffix=".png", prefix="tgbot_latex_")
        _shutil.move(png_path, out_png)
        return out_png, None
    except subprocess.TimeoutExpired:
        return None, "LaTeX 编译超时（>60 秒）"
    except FileNotFoundError as e:
        return None, f"缺少依赖：{e}（请确认已安装 xelatex 和 poppler-utils）"
    except Exception as e:
        return None, f"渲染错误：{e}"
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

def _build_system_prompt(allowed_commands: list[str]) -> str:
    return _SYSTEM_PROMPT_BASE


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
        self._system_prompt = _build_system_prompt(self._allowed_commands)
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
                    "\\usepackage{amsmath,amssymb,geometry,fontenc}\n"
                    "\\geometry{margin=2cm}\n"
                    "\\begin{document}\n"
                    + source +
                    "\n\\end{document}"
                )
            logger.info("Rendering LaTeX (%d chars)", len(source))
            png_path, err = _render_latex(source)
            if err:
                logger.warning("LaTeX render error: %s", err[:200])
                self._telegram_client.send_message(f"⚠️ LaTeX 渲染失败：{err}", parse_mode="HTML")
            else:
                self._telegram_client.send_photo(png_path)
                try:
                    os.unlink(png_path)
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
                self._telegram_client.send_photo(target, caption)
            elif kind == "VIDEO":
                self._telegram_client.send_video(target, caption)
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
        cli_prompt = _build_system_prompt([])  # no tools for cli backend
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
                "name": "render_latex",
                "description": (
                    "Compile a LaTeX document and send the rendered image to the user. "
                    "Use this whenever the user asks for a formatted document, math formulas, "
                    "paper-style layout, or any content that benefits from LaTeX typesetting."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "source": {
                            "type": "string",
                            "description": (
                                "Complete LaTeX source (\\documentclass … \\end{document}). "
                                "Use XeLaTeX-compatible packages. For Chinese text add \\usepackage{xeCJK}. "
                                "For math use amsmath/amssymb. For icons use fontawesome5."
                            ),
                        }
                    },
                    "required": ["source"],
                },
            }
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
        if tool_name == "render_latex":
            source = tool_input.get("source", "")
            logger.info("Tool: render_latex (%d chars)", len(source))
            png_path, err = _render_latex(source)
            if err:
                logger.warning("LaTeX render error: %s", err[:200])
                if self._telegram_client:
                    self._telegram_client.send_message(f"⚠️ LaTeX 渲染失败：{err}", parse_mode="HTML")
                return f"Error: {err}"
            if self._telegram_client:
                self._telegram_client.send_photo(png_path)
            try:
                os.unlink(png_path)
            except OSError:
                pass
            return "Image sent successfully."

        if tool_name == "run_command":
            cmd = tool_input.get("command", "")
            exe = _cmd_executable(cmd)
            if exe not in self._allowed_commands:
                return f"Error: '{exe}' is not in the allowed commands list."
            logger.info("Tool: run_command: %s", cmd)
            return _run_cmd(cmd)

        return f"Error: unknown tool '{tool_name}'"

    def _call_api(self, text: str) -> str:
        # history is already locked by caller
        self._history.append({"role": "user", "content": text})
        max_msgs = self.history_turns * 2
        if len(self._history) > max_msgs:
            self._history = self._history[-max_msgs:]
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

        # Exhausted rounds — return whatever text we have
        raw = "\n".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
        cleaned = self._execute_actions(_convert_md_tables(raw))
        self._history.append({"role": "assistant", "content": response.content})
        return cleaned or "已完成。"

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
                if self.backend == "api" and self._history and self._history[-1]["role"] == "user":
                    self._history.pop()  # roll back poisoned history entry
                logger.warning("Claude error: %s", e)
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
        lines = [
            "<b>📖 使用说明</b>\n",
            "<b>对话</b>",
            "  直接发消息 → 发给 Claude 回答",
            "  <code>?问题</code> → 明确发给 Claude（同上）\n",
            "<b>Shell 命令</b>",
            "  <code>!命令</code> → 在服务器上执行 shell 命令（不允许 sudo）\n",
            "<b>管理命令</b>",
            "  <code>/help</code> — 显示此帮助",
            "  <code>/status</code> — 查看当前 Claude backend 状态",
            "  <code>/clear</code> 或 <code>!clear</code> — 清空 Claude 对话历史",
            "  <code>/setkey &lt;API_KEY&gt;</code> — 设置 Anthropic API key，切换到 api backend",
            "  <code>/setcli</code> — 切回 <code>claude -p</code> backend\n",
            "<b>媒体</b>",
            "  发送图片/视频/文件 → 自动存档到服务器",
            "  让 Claude 发图片或视频 → Claude 会直接发送\n",
            f"<b>当前 Backend：</b><code>{backend}</code>",
        ]
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

    def __init__(self, archive_dir: str, telegram_client):
        self.archive_dir = Path(archive_dir).expanduser()
        self._client = telegram_client

    def handle(self, message: dict) -> str:
        import datetime
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

        if "photo" in message:
            # Telegram sends multiple sizes; pick the largest (last in array)
            file_id = message["photo"][-1]["file_id"]
            save_path = str(self.archive_dir / "photos" / f"{ts}.jpg")
            kind = "图片"
        elif "video" in message:
            file_id = message["video"]["file_id"]
            ext = message["video"].get("mime_type", "video/mp4").split("/")[-1]
            save_path = str(self.archive_dir / "videos" / f"{ts}.{ext}")
            kind = "视频"
        elif "document" in message:
            doc = message["document"]
            file_id = doc["file_id"]
            filename = doc.get("file_name", f"{ts}.bin")
            save_path = str(self.archive_dir / "documents" / filename)
            kind = "文件"
        else:
            return "不支持的媒体类型。"

        logger.info("Archiving %s → %s", kind, save_path)
        ok = self._client.download_file(file_id, save_path)
        if ok:
            return f"✅ {kind}已存档：{save_path}"
        else:
            return f"❌ {kind}存档失败，请查看日志。"
