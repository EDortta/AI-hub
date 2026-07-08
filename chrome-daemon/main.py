#!/usr/bin/env python3
"""AI-Hub Chrome Daemon.

FastAPI HTTP daemon that owns the shared Chrome instance and provides:
- Conversation watching with alias-based routing
- Image generation via ChatGPT GPT models
- Chrome lifecycle management (hidden Xvfb by default)

Port: 9400
"""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import hmac

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# Ensure chrome-daemon dir is on the path when run as a script
sys.path.insert(0, str(Path(__file__).parent))

from chrome_manager import (
    AsyncChromeManager,
    CDP_URL,
    CHROME_PROFILE,
    XVFB_DISPLAY,
    ChromeManager,
    check_chatgpt_session,
    ensure_xvfb,
    invalidate_session_cache,
    mark_chrome_op_end,
    mark_chrome_op_start,
    is_cdp_available,
    kill_chrome,
    launch_chrome,
    launch_visible_chrome,
    playwright_executor,
    run_playwright_async,
)
from watchers import ConversationWatcher, WatcherRegistry, WatcherState, run_chrome_watchdog, run_polling_loop

_LOG_DIR = Path.home() / ".local" / "share" / "ai-hub"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_LOG_FILE = _LOG_DIR / "ai-hub.log"

_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

_file_handler = logging.handlers.RotatingFileHandler(
    _LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_file_handler.setFormatter(_fmt)

_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_fmt)

logging.basicConfig(level=logging.INFO, handlers=[_stream_handler, _file_handler])
log = logging.getLogger("ai-hub.main")


def _reap_children(signum, frame):
    """Collect zombie children so they don't accumulate in the process table."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                break
            log.debug("Reaped child PID %d.", pid)
        except ChildProcessError:
            break


signal.signal(signal.SIGCHLD, _reap_children)

DAEMON_PORT = int(os.environ.get("AI_HUB_PORT", "9400"))

# ---------------------------------------------------------------------------
# Authentication (SEC-0001)
#
# The daemon drives a Chrome instance logged into the user's accounts and can
# execute privileged actions on their behalf. Binding to loopback is not a
# sufficient control on its own (a hostile local process, or a browser page via
# DNS-rebinding, could reach it). Every request must therefore carry a shared
# secret token.
#
# The token is read from the AIHUB_DAEMON_TOKEN environment variable and is
# NEVER hardcoded. Fail-closed: if the variable is unset/empty, all requests are
# rejected so the daemon never runs unauthenticated.
# ---------------------------------------------------------------------------
DAEMON_TOKEN = os.environ.get("AIHUB_DAEMON_TOKEN", "").strip()

# Hostnames accepted in the Host header. Anything else is treated as a possible
# DNS-rebinding attempt and rejected.
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def _host_allowed(host_header: str | None) -> bool:
    if not host_header:
        # No Host header at all is only produced by non-browser clients; allow.
        return True
    host = host_header.split(":", 1)[0].strip().lower()
    return host in _ALLOWED_HOSTS


async def require_auth(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    """FastAPI dependency enforcing shared-token auth on every endpoint."""
    # DNS-rebinding guard: reject unexpected Host headers.
    if not _host_allowed(request.headers.get("host")):
        raise HTTPException(status_code=403, detail="host_not_allowed")

    if not DAEMON_TOKEN:
        # Fail closed: refuse to serve when no token is configured.
        raise HTTPException(status_code=503, detail="daemon_token_not_configured")

    supplied = ""
    if authorization:
        parts = authorization.split(None, 1)
        supplied = parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" else authorization.strip()

    if not supplied or not hmac.compare_digest(supplied, DAEMON_TOKEN):
        raise HTTPException(status_code=401, detail="invalid_or_missing_token")


# ---------------------------------------------------------------------------
# Path confinement (SEC-0108)
#
# reference_image_path / image_path / output_dir are caller-controlled and are
# either read and uploaded to an external site (ChatGPT/X/LinkedIn) or written
# to. Without confinement a caller could point at any user-readable file (e.g.
# ~/.ssh/id_rsa) for exfiltration, or write output anywhere on disk. Every path
# is resolved and rejected unless it falls under one of the allowed base dirs.
# ---------------------------------------------------------------------------
_DEFAULT_ALLOWED_PATHS = (
    Path.home() / ".local" / "share" / "ai-hub",
    Path.home() / "Sync" / "Projects",
)
_ALLOWED_BASE_PATHS = [
    Path(p).expanduser().resolve()
    for p in os.environ.get("AIHUB_ALLOWED_PATHS", "").split(":")
    if p.strip()
] or [p.resolve() for p in _DEFAULT_ALLOWED_PATHS]


def _confine_path(raw: str, *, field: str) -> Path:
    """Resolve `raw` and reject it if outside the allowed base directories."""
    candidate = Path(raw).expanduser().resolve(strict=False)
    if not any(candidate == base or candidate.is_relative_to(base) for base in _ALLOWED_BASE_PATHS):
        raise HTTPException(status_code=400, detail=f"{field}_outside_allowed_paths")
    return candidate


registry = WatcherRegistry()

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

_login_in_progress: bool = False
# Semaphore initialized in lifespan (must bind to the running event loop).
_chrome_op_sem: asyncio.Semaphore | None = None


def _chrome_manager_factory():
    return ChromeManager(cdp_url=CDP_URL)


def _require_not_login_in_progress() -> None:
    if _login_in_progress:
        raise HTTPException(status_code=503, detail="login_in_progress")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _chrome_op_sem
    _chrome_op_sem = asyncio.Semaphore(1)

    # SEC-0001: fail-closed auth. Without a token every request is rejected.
    if not DAEMON_TOKEN:
        log.critical(
            "AIHUB_DAEMON_TOKEN is not set — all endpoints will reject requests "
            "with 503. Export AIHUB_DAEMON_TOKEN to enable the daemon."
        )
    else:
        log.info("Daemon auth enabled (shared token via AIHUB_DAEMON_TOKEN).")

    # Startup: ensure Xvfb and Chrome
    display = ensure_xvfb(XVFB_DISPLAY)
    launch_chrome(profile_dir=CHROME_PROFILE, display=display, cdp_url=CDP_URL)
    log.info("Chrome ready at %s", CDP_URL)

    # Start polling loop and Chrome watchdog in background
    task = asyncio.create_task(run_polling_loop(registry, _chrome_manager_factory))
    watchdog = asyncio.create_task(run_chrome_watchdog())
    log.info("Polling loop and Chrome watchdog started.")

    yield

    task.cancel()
    watchdog.cancel()
    log.info("AI-Hub daemon stopped.")


app = FastAPI(
    title="AI-Hub Chrome Daemon",
    version="1.0.0",
    lifespan=lifespan,
    # SEC-0001: enforce shared-token auth (+ Host guard) on every endpoint.
    dependencies=[Depends(require_auth)],
)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@app.get("/status")
async def status():
    from chrome_manager import _session_cache
    return {
        "ok": True,
        "chrome_cdp_available": is_cdp_available(CDP_URL),
        "cdp_url": CDP_URL,
        "watchers": len(registry.all()),
        "display": os.environ.get("DISPLAY", XVFB_DISPLAY),
        "login_in_progress": _login_in_progress,
        "chatgpt_logged_in": _session_cache.get("ok"),
    }


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

@app.get("/session/check")
async def session_check(gpt_url: str = ""):
    """Check if ChatGPT is logged in. Forces a live check (bypasses cache)."""
    invalidate_session_cache()
    logged_in = await run_playwright_async(
        lambda: check_chatgpt_session(CDP_URL, gpt_url), timeout=30
    )
    return {"logged_in": logged_in}


class LoginRequest(BaseModel):
    display: str = ":0"


@app.post("/session/login")
async def session_login(req: LoginRequest):
    """Stop headless Chrome and open a visible window for manual ChatGPT login.

    Call POST /session/login-done when the user has finished logging in.
    """
    global _login_in_progress
    if _login_in_progress:
        raise HTTPException(status_code=409, detail="Login already in progress.")
    if _chrome_op_sem is None:
        raise HTTPException(status_code=503, detail="Daemon not fully started.")

    # Wait for any active generation/publish to complete (up to 30s).
    try:
        await asyncio.wait_for(_chrome_op_sem.acquire(), timeout=30)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=503,
            detail="Timed out waiting for active operation to finish.",
        )

    _login_in_progress = True
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, lambda: kill_chrome(CHROME_PROFILE))
        await asyncio.sleep(1)
        # Launch Chrome on the user's real display (not Xvfb).
        await loop.run_in_executor(
            None,
            lambda: launch_visible_chrome(CHROME_PROFILE, CDP_URL, req.display),
        )
        log.info("Visible Chrome opened on display=%s for login.", req.display)
    except Exception as exc:
        _login_in_progress = False
        _chrome_op_sem.release()
        raise HTTPException(status_code=500, detail=str(exc))

    # Semaphore stays acquired until /session/login-done releases it.
    return {"ok": True, "message": f"Chrome aberto em display={req.display}. Faça login e chame POST /session/login-done."}


@app.post("/session/login-done")
async def session_login_done():
    """Close the visible Chrome and restart headless. Call after manual login."""
    global _login_in_progress
    if not _login_in_progress:
        raise HTTPException(status_code=400, detail="No login in progress.")

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, lambda: kill_chrome(CHROME_PROFILE))
        await asyncio.sleep(1)
        display = await loop.run_in_executor(None, lambda: ensure_xvfb(XVFB_DISPLAY))
        await loop.run_in_executor(
            None,
            lambda: launch_chrome(profile_dir=CHROME_PROFILE, display=display, cdp_url=CDP_URL),
        )
        invalidate_session_cache()
        log.info("Headless Chrome restarted after login.")
    finally:
        _login_in_progress = False
        if _chrome_op_sem is not None:
            _chrome_op_sem.release()

    return {"ok": True, "message": "Headless Chrome reiniciado. Sessão pronta."}


# Legacy alias kept for backwards compatibility.
@app.get("/setup")
async def setup():
    """Deprecated — use POST /session/login instead."""
    return {"ok": False, "message": "Use POST /session/login (display param) instead of /setup."}


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    url: str
    alias: str = ""
    chatgpt_alias: str = ""
    purpose: str = ""
    interaction_poll_seconds: int = 5
    latency_poll_seconds: int = 60
    callback_url: str = ""
    project_path: str = ""


@app.post("/conversations/register", status_code=201)
async def register_conversation(req: RegisterRequest):
    w = ConversationWatcher(
        url=req.url,
        alias=req.alias,
        chatgpt_alias=req.chatgpt_alias,
        purpose=req.purpose,
        interaction_poll_seconds=req.interaction_poll_seconds,
        latency_poll_seconds=req.latency_poll_seconds,
        callback_url=req.callback_url,
        project_path=req.project_path,
    )
    registry.register(w)
    return w.to_dict()


@app.delete("/conversations/{watcher_id}")
async def unregister_conversation(watcher_id: str):
    if not registry.unregister(watcher_id):
        raise HTTPException(status_code=404, detail="Watcher not found")
    return {"ok": True}


@app.delete("/conversations/by-project/{project_path:path}")
async def unregister_by_project(project_path: str):
    count = registry.unregister_by_project(project_path)
    return {"ok": True, "removed": count}


@app.get("/conversations")
async def list_conversations():
    return [w.to_dict() for w in registry.all()]


class SendRequest(BaseModel):
    text: str


@app.post("/conversations/{watcher_id}/send")
async def send_message(watcher_id: str, req: SendRequest):
    """Post a message to the ChatGPT conversation owned by this watcher."""
    w = registry.get(watcher_id)
    if not w:
        raise HTTPException(status_code=404, detail="Watcher not found")

    async with AsyncChromeManager(cdp_url=CDP_URL) as mgr:
        ok = await mgr.send_message(w.url, req.text)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to send message to ChatGPT page")
    return {"ok": True}


@app.get("/conversations/{watcher_id}/inbox")
async def get_inbox(watcher_id: str):
    """Return all pending inbox messages for this watcher (addressed to its alias)."""
    w = registry.get(watcher_id)
    if not w:
        raise HTTPException(status_code=404, detail="Watcher not found")
    return {"inbox": list(w.inbox)}


@app.delete("/conversations/{watcher_id}/inbox")
async def clear_inbox(watcher_id: str):
    """Clear all inbox messages for this watcher."""
    w = registry.get(watcher_id)
    if not w:
        raise HTTPException(status_code=404, detail="Watcher not found")
    w.inbox.clear()
    return {"ok": True}


@app.get("/conversations/{watcher_id}/last-message")
async def get_last_message(watcher_id: str):
    """Return the last assistant message in the watcher's conversation."""
    from watchers import _async_expand_and_extract

    w = registry.get(watcher_id)
    if not w:
        raise HTTPException(status_code=404, detail="Watcher not found")

    async with AsyncChromeManager(cdp_url=CDP_URL) as mgr:
        page = await mgr.get_or_open_page(w.url)
        await page.keyboard.press("Home")
        await page.wait_for_timeout(2_000)
        messages = await _async_expand_and_extract(page)

    last_assistant = next((m for m in reversed(messages) if m.get("role") == "assistant"), None)
    return {"message": last_assistant}


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

@app.get("/debug/screenshot")
async def debug_screenshot(url_contains: str = "chatgpt"):
    """Save a screenshot of the matching Chrome page for visual debugging."""
    import base64
    from pathlib import Path as _Path

    out = _Path.home() / ".local" / "share" / "ai-hub" / "debug-screenshot.png"
    out.parent.mkdir(parents=True, exist_ok=True)

    async with AsyncChromeManager(cdp_url=CDP_URL) as mgr:
        ctx = mgr.context
        if ctx is None:
            raise HTTPException(status_code=503, detail="No browser context")
        pages = ctx.pages
        page = next((p for p in pages if url_contains in (p.url or "")), None)
        if page is None:
            urls = [p.url for p in pages]
            raise HTTPException(status_code=404, detail=f"No page matching '{url_contains}'. Open: {urls}")
        await page.screenshot(path=str(out), full_page=False)

    return {"ok": True, "saved": str(out)}


@app.post("/browse")
async def browse(url: str, wait_ms: int = 3000):
    """Navega para uma URL usando o ChromeManager compartilhado e retorna screenshot em base64."""
    import base64
    from pathlib import Path as _Path

    out = _Path.home() / ".local" / "share" / "ai-hub" / "browse-screenshot.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    out_str = str(out)
    wait_sec = wait_ms / 1000

    def _do_browse(cdp_url: str):
        with ChromeManager(cdp_url=cdp_url) as mgr:
            page = mgr.context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(wait_ms)
                page.screenshot(path=out_str, full_page=False)
                title = page.title()
                content = page.evaluate(
                    "Array.from(document.querySelectorAll('a[href],button'))"
                    ".map(e=>(e.innerText||'').trim()+'-->'+(e.href||''))"
                    ".filter(t=>t.length>3&&t.length<150).slice(0,40).join('|')"
                )
                return {"title": title, "content": content}
            finally:
                page.close()

    result = await run_playwright_async(lambda: _do_browse(CDP_URL), timeout=40)
    img_b64 = base64.b64encode(out.read_bytes()).decode()
    return {"ok": True, "title": result["title"], "elements": result["content"],
            "screenshot_b64": img_b64, "saved": out_str}


@app.post("/page/action")
async def page_action(url: str, action: str, selector: str = "", value: str = "", wait_ms: int = 2000):
    """Executa ação numa página aberta: click, type, evaluate."""
    async with AsyncChromeManager(cdp_url=CDP_URL) as mgr:
        ctx = mgr.context
        if ctx is None:
            raise HTTPException(status_code=503, detail="No browser context")
        pages = ctx.pages
        page = next((p for p in pages if url in (p.url or "")), None)
        if page is None:
            raise HTTPException(status_code=404, detail=f"Página não encontrada: {url}")

        if action == "click":
            await page.click(selector)
        elif action == "type":
            await page.fill(selector, value)
        elif action == "evaluate":
            result = await page.evaluate(selector)
            return {"ok": True, "result": result}
        elif action == "screenshot":
            import base64
            from pathlib import Path as _Path
            out = _Path.home() / ".local" / "share" / "ai-hub" / "action-screenshot.png"
            await page.screenshot(path=str(out))
            img_b64 = base64.b64encode(out.read_bytes()).decode()
            return {"ok": True, "screenshot_b64": img_b64}

        await page.wait_for_timeout(wait_ms)
        title = await page.title()
        cur_url = page.url
        return {"ok": True, "title": title, "url": cur_url}


# ---------------------------------------------------------------------------
# Image generation
# ---------------------------------------------------------------------------

class ImageRequest(BaseModel):
    gpt_url: str
    prompt: str
    orientation: str = "portrait"
    output_dir: str = ""
    greeting: str = "Hey, "
    reference_image_path: str = ""
    delete_chat: bool = False


@app.post("/image/generate")
async def generate_image(req: ImageRequest):
    from image_generator import DEFAULT_OUTPUT_DIR, generate_image as _gen

    _require_not_login_in_progress()
    if _chrome_op_sem is None:
        raise HTTPException(status_code=503, detail="Daemon not fully started.")

    output_dir = _confine_path(req.output_dir, field="output_dir") if req.output_dir else DEFAULT_OUTPUT_DIR
    ref_path = _confine_path(req.reference_image_path, field="reference_image_path") if req.reference_image_path else None
    if ref_path is not None and not ref_path.is_file():
        raise HTTPException(status_code=400, detail=f"reference_image_path not found: {ref_path}")

    async with _chrome_op_sem:
        # Mark the operation in-flight so the Chrome watchdog does not treat the
        # high CPU of a legitimate generation as "stale" and kill Chrome. See issue 001.
        mark_chrome_op_start()
        try:
            # Session check (uses TTL cache — no overhead on repeated calls).
            logged_in = await run_playwright_async(
                lambda: check_chatgpt_session(CDP_URL, req.gpt_url), timeout=30
            )
            if not logged_in:
                log.warning("generate_image blocked: ChatGPT session expired.")
                raise HTTPException(status_code=401, detail="chatgpt_session_expired")

            try:
                image_path = await run_playwright_async(
                    lambda: _gen(
                        gpt_url=req.gpt_url,
                        prompt=req.prompt,
                        orientation=req.orientation,
                        output_dir=output_dir,
                        greeting=req.greeting,
                        cdp_url=CDP_URL,
                        reference_image_path=ref_path,
                        delete_chat=req.delete_chat,
                    ),
                    timeout=700,
                )
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))
        finally:
            mark_chrome_op_end()

    return {"ok": True, "image_path": str(image_path)}


class DeleteChatRequest(BaseModel):
    url: str


@app.post("/page/delete-chat")
async def delete_chat(req: DeleteChatRequest):
    """Best-effort delete of the ChatGPT conversation open at the given URL.

    Idempotent and non-fatal: returns {"ok": True, "deleted": false} if the UI
    action could not be performed (issue 003).
    """
    from image_generator import delete_current_chat

    _require_not_login_in_progress()
    if _chrome_op_sem is None:
        raise HTTPException(status_code=503, detail="Daemon not fully started.")

    def _do_delete(cdp_url: str) -> bool:
        with ChromeManager(cdp_url=cdp_url) as mgr:
            page = mgr.get_or_open_page(req.url)
            return delete_current_chat(page)

    async with _chrome_op_sem:
        mark_chrome_op_start()
        try:
            deleted = await run_playwright_async(lambda: _do_delete(CDP_URL), timeout=60)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            mark_chrome_op_end()

    return {"ok": True, "deleted": bool(deleted)}


# ---------------------------------------------------------------------------
# Social publishing
# ---------------------------------------------------------------------------

class PublishSocialRequest(BaseModel):
    image_path: str
    caption: str
    url: str


@app.post("/social/publish/x")
async def publish_to_x(req: PublishSocialRequest):
    from social_publisher import publish_to_x as _pub

    _require_not_login_in_progress()
    image_path = _confine_path(req.image_path, field="image_path")
    if not image_path.exists():
        raise HTTPException(status_code=400, detail=f"Image not found: {image_path}")

    mark_chrome_op_start()
    try:
        await run_playwright_async(
            lambda: _pub(image_path, req.caption, req.url, CDP_URL),
            timeout=120,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        mark_chrome_op_end()

    return {"ok": True}


@app.post("/social/publish/linkedin")
async def publish_to_linkedin(req: PublishSocialRequest):
    from social_publisher import publish_to_linkedin as _pub

    _require_not_login_in_progress()
    image_path = _confine_path(req.image_path, field="image_path")
    if not image_path.exists():
        raise HTTPException(status_code=400, detail=f"Image not found: {image_path}")

    mark_chrome_op_start()
    try:
        await run_playwright_async(
            lambda: _pub(image_path, req.caption, req.url, CDP_URL),
            timeout=300,
        )
    except Exception as e:
        log.error("publish_to_linkedin failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        mark_chrome_op_end()

    return {"ok": True}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=DAEMON_PORT,
        log_level="info",
        reload=False,
    )
