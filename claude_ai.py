"""Shared plumbing for asking Claude, via the Claude Agent SDK (which runs the installed Claude Code).

Used by propose_ai.py (photo/text -> proposed log entries) and ask_ai.py (questions about the log).

How auth works (no code here ever touches a credential):
  The SDK starts the unmodified Claude Code program, which uses whatever sign-in Claude Code
  itself has: your own `claude auth login` (stored by Claude Code in ~/.claude), or
  ANTHROPIC_API_KEY if that is set in the server's environment (API credits). Switching
  between the two is an environment change only.

Privacy:
  Photos are shrunk and re-encoded in memory (location/EXIF data dropped), sent once, and
  discarded. Claude Code runs in an empty folder, with no built-in tools, none of your
  personal Claude Code settings, and session persistence off, so it keeps no transcript.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import io
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, CLINotFoundError, ResultMessage, query
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_EDGE = 1024                  # px, long edge sent to Claude
MAX_UPLOAD = 15 * 1024 * 1024    # bytes of image accepted from the browser
MAX_PIXELS = 50_000_000          # refuse bigger images before decoding them
FORMATS = ["JPEG", "PNG", "WEBP"]   # never hand other formats (e.g. EPS -> Ghostscript) to Pillow

# Extra settings for the Claude Code run: no session-title request, no telemetry, error
# reports, or prompt history. Merged on top of the server's own environment.
CLAUDE_ENV = {
    "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",
}

# The subscription login must stay inside Claude Code's own store, never in the app's environment.
for _name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"):
    if os.environ.get(_name):
        print(f"WARNING: {_name} is set in the baby tracker's environment. Remove it and use "
              "`claude auth login` (or ANTHROPIC_API_KEY for API credits).", flush=True)

# Claude Code runs in an empty folder so it never sees project files.
WORK_DIR = Path(os.environ.get("BABY_DATA_DIR", Path(__file__).resolve().parent / "data")) / "claude-workdir"
_slots = threading.BoundedSemaphore(2)   # at most two Claude runs in flight


class AIError(Exception):
    """A failure with a message the parent can act on, and the HTTP status to return."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


@dataclass
class ClaudeResult:
    text: str                  # Claude's final text (with output_schema: the JSON as text)
    structured: Any            # parsed structured output when output_schema was given, else None
    elapsed_ms: int
    num_turns: int
    cost_usd: float | None     # what the run would cost on API pricing (reported even on a subscription)
    model: str                 # the model alias or id that was requested


def prepare_image(raw: bytes) -> bytes:
    """Shrink to MAX_EDGE on the long side and re-encode as JPEG. Drops EXIF (location, etc.)."""
    if not raw:
        raise AIError("No photo received", 400)
    if len(raw) > MAX_UPLOAD:
        raise AIError("Photo is too large (max 15 MB)", 413)
    try:
        with Image.open(io.BytesIO(raw), formats=FORMATS) as im:
            if im.width * im.height > MAX_PIXELS:
                raise AIError("Photo has too many pixels (max 50 megapixels)", 413)
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            im.save(out, "JPEG", quality=85, optimize=True)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError):
        raise AIError("Couldn't read that image. Try a JPEG or PNG photo.", 400)
    return out.getvalue()


def image_block(jpeg: bytes) -> dict:
    """A content block for run_claude() holding one JPEG (already passed through prepare_image)."""
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.b64encode(jpeg).decode()}}


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def run_claude(
    content: list[dict] | str,
    *,
    system_prompt: str,
    model: str,
    timeout_s: float,
    max_turns: int = 1,
    output_schema: dict | None = None,
    mcp_servers: dict | None = None,
    allowed_tools: list[str] | None = None,
    effort: str | None = None,
) -> ClaudeResult:
    """Blocking: one Claude Code run, returning its final answer. Raises AIError on any failure.

    content:        the user message: a string, or a list of content blocks (image_block, text_block).
    output_schema:  JSON Schema; Claude must answer with matching JSON (ClaudeResult.structured).
    mcp_servers:    in-process tools, e.g. {"log": create_sdk_mcp_server(...)}; list the tool names
                    you want callable in allowed_tools (e.g. ["mcp__log__sql"]). Built-in tools
                    (Bash, Read, ...) are never available.
    """
    if not _slots.acquire(timeout=5):
        raise AIError("Claude is still busy with another request. Try again in a moment.", 429)
    started = time.monotonic()
    try:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        coro = _run(content, system_prompt=system_prompt, model=model, max_turns=max_turns,
                    output_schema=output_schema, mcp_servers=mcp_servers or {},
                    allowed_tools=allowed_tools or [], effort=effort)
        result = asyncio.run(asyncio.wait_for(coro, timeout_s))
    except asyncio.TimeoutError:
        raise AIError(f"Claude didn't answer within {timeout_s:.0f} s. Try again.", 504)
    finally:
        _slots.release()
    if output_schema is not None and result.structured_output is None:
        raise AIError("Claude's answer wasn't in the expected format. Try again.")
    return ClaudeResult(
        text=result.result or "",
        structured=result.structured_output,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        num_turns=result.num_turns,
        cost_usd=result.total_cost_usd,
        model=model,
    )


async def _run(content, *, system_prompt, model, max_turns, output_schema, mcp_servers, allowed_tools, effort):
    if isinstance(content, str):
        content = [text_block(content)]

    async def messages():
        # Images need streaming input mode: one user message with content blocks.
        yield {"type": "user", "message": {"role": "user", "content": content}}

    stderr_tail: collections.deque[str] = collections.deque(maxlen=20)   # last lines only
    options = ClaudeAgentOptions(
        model=model,
        max_turns=max_turns,
        tools=[],                       # no built-in tools at all (Bash, Read, Edit, ...)
        mcp_servers=mcp_servers,        # only our own in-process tools, if any
        allowed_tools=allowed_tools,
        system_prompt=system_prompt,    # replaces Claude Code's coding-assistant prompt
        setting_sources=[],             # ignore ~/.claude settings, CLAUDE.md, hooks, plugins
        strict_mcp_config=True,         # and any configured MCP servers
        permission_mode="dontAsk",      # anything not in allowed_tools is refused
        cwd=str(WORK_DIR),
        extra_args={"no-session-persistence": None},   # keep no transcript (it would hold the photo)
        env=CLAUDE_ENV,
        stderr=lambda line: stderr_tail.append(line[-300:]),
        output_format={"type": "json_schema", "schema": output_schema} if output_schema else None,
        effort=effort,
    )
    result: ResultMessage | None = None
    kind: str | None = None   # structured error from Claude Code, e.g. "authentication_failed"
    try:
        async for msg in query(prompt=messages(), options=options):
            if isinstance(msg, AssistantMessage) and msg.error:
                kind = msg.error
            elif isinstance(msg, ResultMessage):
                result = msg
    except CLINotFoundError:
        raise AIError("Claude Code isn't installed for the server. See README: Claude features > Setup.", 503)
    except AIError:
        raise
    except Exception as e:
        raise AIError(_explain(f"{e} {' '.join(stderr_tail)}", kind))
    if result is None:
        raise AIError(_explain(" ".join(stderr_tail) or "Claude Code ended without an answer", kind))
    if result.subtype == "error_max_turns":
        raise AIError("Claude needed too many steps for this one. Try a simpler question.")
    if result.is_error or result.subtype != "success":
        detail = " ".join([result.result or "", *(result.errors or []), result.subtype])
        raise AIError(_explain(detail, kind, result.api_error_status))
    return result


def _explain(detail: str, kind: str | None = None, status: int | None = None) -> str:
    """Map a failure to a message that says what to do. Structured signals first, then text."""
    d = detail.lower()
    if "maximum number of turns" in d:   # Claude Code exits with an error at the turn limit
        return "Claude needed too many steps for this one. Try a simpler question."
    if kind == "rate_limit" or status == 429 or re.search(r"usage limit|rate limit|hit your limit|\b429\b", d):
        return "Claude usage limit reached. Try again later."
    if kind == "authentication_failed" or status in (401, 403) or re.search(
            r"not logged in|please run /login|login expired|invalid api key|oauth token has expired|\b401\b", d):
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "Claude rejected the API key (ANTHROPIC_API_KEY in ~/.config/baby-tracker/env)."
        return "Claude login is missing or expired. On the VM run: claude auth login"
    if kind == "billing_error" or status == 402:
        return "Claude billing problem: check your plan or API credits."
    if kind == "server_error" or re.search(r"overloaded|\b5\d\d\b", d):
        return "Claude is busy right now. Try again in a minute."
    return "Claude couldn't answer: " + " ".join(detail.split())[:200]
