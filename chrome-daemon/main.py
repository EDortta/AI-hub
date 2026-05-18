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
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# Ensure chrome-daemon dir is on the path when run as a script
sys.path.insert(0, str(Path(__file__).parent))

from chrome_manager import (
    CDP_URL,
    CHROME_PROFILE,
    XVFB_DISPLAY,
    ChromeManager,
    ensure_xvfb,
    is_cdp_available,
    launch_chrome,
    launch_visible_chrome,
)
from watchers import ConversationWatcher, WatcherRegistry, WatcherState, run_polling_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("ai-hub.main")

DAEMON_PORT = int(os.environ.get("AI_HUB_PORT", "9400"))

registry = WatcherRegistry()


def _chrome_manager_factory():
    return ChromeManager(cdp_url=CDP_URL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: ensure Xvfb and Chrome
    display = ensure_xvfb(XVFB_DISPLAY)
    launch_chrome(profile_dir=CHROME_PROFILE, display=display, cdp_url=CDP_URL)
    log.info("Chrome ready at %s", CDP_URL)

    # Start polling loop in background
    task = asyncio.create_task(run_polling_loop(registry, _chrome_manager_factory))
    log.info("Polling loop started.")

    yield

    task.cancel()
    log.info("AI-Hub daemon stopped.")


app = FastAPI(title="AI-Hub Chrome Daemon", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@app.get("/status")
async def status():
    return {
        "ok": True,
        "chrome_cdp_available": is_cdp_available(CDP_URL),
        "cdp_url": CDP_URL,
        "watchers": len(registry.all()),
        "display": os.environ.get("DISPLAY", XVFB_DISPLAY),
    }


# ---------------------------------------------------------------------------
# Setup (show Chrome for manual login)
# ---------------------------------------------------------------------------

@app.get("/setup")
async def setup():
    """Launch Chrome visibly so the user can log in to ChatGPT."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, launch_visible_chrome)
    return {"ok": True, "message": "Chrome aberto no display real para login manual."}


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

    loop = asyncio.get_event_loop()

    def _sync_send():
        with ChromeManager(cdp_url=CDP_URL) as mgr:
            return mgr.send_message(w.url, req.text)

    ok = await loop.run_in_executor(None, _sync_send)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to send message to ChatGPT page")
    return {"ok": True}


@app.get("/conversations/{watcher_id}/last-message")
async def get_last_message(watcher_id: str):
    """Return the last assistant message in the watcher's conversation."""
    from watchers import _expand_and_extract

    w = registry.get(watcher_id)
    if not w:
        raise HTTPException(status_code=404, detail="Watcher not found")

    loop = asyncio.get_event_loop()

    def _sync_peek():
        with ChromeManager(cdp_url=CDP_URL) as mgr:
            page = mgr.get_or_open_page(w.url)
            page.keyboard.press("Home")
            page.wait_for_timeout(2_000)
            return _expand_and_extract(page)

    messages = await loop.run_in_executor(None, _sync_peek)
    last_assistant = next((m for m in reversed(messages) if m.get("role") == "assistant"), None)
    return {"message": last_assistant}


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


@app.post("/image/generate")
async def generate_image(req: ImageRequest):
    from image_generator import DEFAULT_OUTPUT_DIR, generate_image as _gen

    output_dir = Path(req.output_dir).expanduser() if req.output_dir else DEFAULT_OUTPUT_DIR
    ref_path = Path(req.reference_image_path).expanduser() if req.reference_image_path else None

    loop = asyncio.get_event_loop()
    try:
        image_path = await loop.run_in_executor(
            None,
            lambda: _gen(
                gpt_url=req.gpt_url,
                prompt=req.prompt,
                orientation=req.orientation,
                output_dir=output_dir,
                greeting=req.greeting,
                cdp_url=CDP_URL,
                reference_image_path=ref_path,
            ),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"ok": True, "image_path": str(image_path)}


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

    image_path = Path(req.image_path).expanduser()
    if not image_path.exists():
        raise HTTPException(status_code=400, detail=f"Image not found: {image_path}")

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(
            None,
            lambda: _pub(image_path, req.caption, req.url, CDP_URL),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"ok": True}


@app.post("/social/publish/linkedin")
async def publish_to_linkedin(req: PublishSocialRequest):
    from social_publisher import publish_to_linkedin as _pub

    image_path = Path(req.image_path).expanduser()
    if not image_path.exists():
        raise HTTPException(status_code=400, detail=f"Image not found: {image_path}")

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(
            None,
            lambda: _pub(image_path, req.caption, req.url, CDP_URL),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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
