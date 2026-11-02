"""Classify a baby photo into a tracker activity with Claude, via the Claude Agent SDK.

How auth works (no code here ever touches a credential):
  The SDK starts the unmodified Claude Code program, which uses whatever sign-in Claude Code
  itself has: your own `claude auth login` (stored by Claude Code in ~/.claude), or
  ANTHROPIC_API_KEY if that is set in the server's environment (API credits). Switching
  between the two is an environment change only.

Privacy:
  Photos are shrunk and re-encoded in memory (location/EXIF data dropped), sent once, and
  discarded. Claude Code is run with session persistence off, so it keeps no transcript.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import io
import json
import math
import os
import re
import threading
import time
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, CLINotFoundError, ResultMessage, query
from PIL import Image, ImageOps, UnidentifiedImageError

MODEL = os.environ.get("BABY_PHOTO_MODEL", "haiku")
TIMEOUT_S = float(os.environ.get("BABY_PHOTO_TIMEOUT", "30"))   # stopping Claude Code adds up to ~10 s
MAX_EDGE = 1024                  # px, long edge sent to Claude
MAX_UPLOAD = 15 * 1024 * 1024    # bytes accepted from the browser
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

ACTIVITIES = ("feeding", "sleeping", "playing", "diaper_change", "tummy_time", "bath", "other")
ALIASES = {
    "feed": "feeding", "nursing": "feeding", "breastfeeding": "feeding", "bottle": "feeding",
    "bottle feeding": "feeding", "eating": "feeding",
    "sleep": "sleeping", "asleep": "sleeping", "napping": "sleeping", "nap": "sleeping",
    "play": "playing", "playtime": "playing",
    "diaper": "diaper_change", "diaper change": "diaper_change", "changing": "diaper_change",
    "tummy time": "tummy_time", "tummy": "tummy_time",
    "bathing": "bath", "bath time": "bath", "bathtime": "bath",
}

SYSTEM_PROMPT = """You label one photo for a parent's private baby-activity log. You are a classifier, not an assistant.

Reply with ONLY one JSON object, no prose and no code fences:
{"activity": "<label>", "confidence": <number 0 to 1>, "notes": "<at most 15 words on what you see>"}

<label> is exactly one of:
- feeding: breastfeeding, bottle feeding, or spoon feeding
- sleeping: baby asleep (eyes closed, resting in a crib, bassinet, stroller, or someone's arms)
- playing: awake baby engaged with toys, books, or people (not on its tummy)
- diaper_change: baby on a changing pad or a diaper being changed
- tummy_time: awake baby lying on its stomach
- bath: baby in a tub, sink, or water
- other: anything else, or no baby visible

If unsure, pick the most likely label and lower the confidence. Do not identify or name people."""

USER_TEXT = "Classify this photo."

# Claude Code runs in an empty folder so it never sees project files.
WORK_DIR = Path(os.environ.get("BABY_DATA_DIR", Path(__file__).resolve().parent / "data")) / "claude-workdir"
_slots = threading.BoundedSemaphore(2)   # at most two photos in flight


class PhotoError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def prepare_image(raw: bytes) -> bytes:
    """Shrink to MAX_EDGE on the long side and re-encode as JPEG. Drops EXIF (location, etc.)."""
    if not raw:
        raise PhotoError("No photo received", 400)
    if len(raw) > MAX_UPLOAD:
        raise PhotoError("Photo is too large (max 15 MB)", 413)
    try:
        with Image.open(io.BytesIO(raw), formats=FORMATS) as im:
            if im.width * im.height > MAX_PIXELS:
                raise PhotoError("Photo has too many pixels (max 50 megapixels)", 413)
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            im.save(out, "JPEG", quality=85, optimize=True)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError):
        raise PhotoError("Couldn't read that image. Try a JPEG or PNG photo.", 400)
    return out.getvalue()


def parse_reply(text: str | None) -> dict:
    """Turn Claude's reply into {activity, confidence, notes, parsed}. Falls back to 'other'."""
    fallback = {"activity": "other", "confidence": 0.0, "notes": "", "parsed": False}
    obj = _first_answer(text or "")
    if obj is None:
        return fallback
    label = " ".join(str(obj.get("activity", "")).lower().replace("_", " ").replace("-", " ").split())
    activity = label.replace(" ", "_") if label.replace(" ", "_") in ACTIVITIES else ALIASES.get(label)
    raw_conf = obj.get("confidence", 0)
    try:
        conf = 0.0 if isinstance(raw_conf, bool) else float(raw_conf)
    except (TypeError, ValueError):
        conf = 0.0
    if not math.isfinite(conf):
        conf = 0.0
    if 2 <= conf <= 100:         # "85" meaning 85%
        conf /= 100
    conf = max(0.0, min(1.0, conf))
    notes = " ".join(str(obj.get("notes", "")).split())[:200]
    if activity is None:
        return {**fallback, "notes": notes}
    return {"activity": activity, "confidence": round(conf, 2), "notes": notes, "parsed": True}


def _first_answer(text: str) -> dict | None:
    """The first JSON object in the reply that has an "activity" key (ignores prose and extras)."""
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text, m.start())
        except ValueError:
            continue
        if isinstance(obj, dict) and "activity" in obj:
            return obj
    return None


async def _ask_claude(jpeg: bytes) -> str:
    async def messages():
        # Images need streaming input mode: one user message with an image block.
        yield {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                 "data": base64.b64encode(jpeg).decode()}},
                    {"type": "text", "text": USER_TEXT},
                ],
            },
        }

    stderr_tail: collections.deque[str] = collections.deque(maxlen=20)   # last lines only
    options = ClaudeAgentOptions(
        model=MODEL,
        max_turns=1,
        tools=[],                       # no built-in tools at all
        system_prompt=SYSTEM_PROMPT,    # replaces Claude Code's coding-assistant prompt
        setting_sources=[],             # ignore ~/.claude settings, CLAUDE.md, hooks, plugins
        strict_mcp_config=True,         # and any configured MCP servers
        permission_mode="dontAsk",
        cwd=str(WORK_DIR),
        extra_args={"no-session-persistence": None},   # keep no transcript (it would hold the photo)
        env=CLAUDE_ENV,
        stderr=lambda line: stderr_tail.append(line[-300:]),
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
        raise PhotoError("Claude Code isn't installed for the server. See README: Log from a photo > Setup.", 503)
    except Exception as e:
        raise PhotoError(_explain(f"{e} {' '.join(stderr_tail)}", kind))
    if result is None:
        raise PhotoError(_explain(" ".join(stderr_tail) or "Claude Code ended without an answer", kind))
    if result.is_error or result.subtype != "success":
        detail = " ".join([result.result or "", *(result.errors or []), result.subtype])
        raise PhotoError(_explain(detail, kind, result.api_error_status))
    return result.result or ""


def _explain(detail: str, kind: str | None = None, status: int | None = None) -> str:
    """Map a failure to a message that says what to do. Structured signals first, then text."""
    d = detail.lower()
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
    return "Claude couldn't classify the photo: " + " ".join(detail.split())[:200]


def classify(raw: bytes) -> dict:
    """Blocking entry point for the HTTP server: returns the parsed suggestion plus timing."""
    if not _slots.acquire(timeout=5):   # taken before decoding, so big images can't pile up
        raise PhotoError("Another photo is still being checked. Try again in a moment.", 429)
    try:
        jpeg = prepare_image(raw)
        del raw
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        text = asyncio.run(asyncio.wait_for(_ask_claude(jpeg), TIMEOUT_S))
    except asyncio.TimeoutError:
        raise PhotoError(f"Claude didn't answer within {TIMEOUT_S:.0f} s. Try again.", 504)
    finally:
        _slots.release()
    out = parse_reply(text)
    out["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    out["model"] = MODEL
    return out
