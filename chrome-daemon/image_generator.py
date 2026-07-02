"""Image generation via ChatGPT GPT models.

Extracted from IgrejaPequena/sofia.py and Dopamin Captain/daily_post.py.
Uses the shared Chrome instance managed by chrome_manager.
"""
from __future__ import annotations

import io
import logging
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("ai-hub.image")

DEFAULT_OUTPUT_DIR = Path.home() / ".local" / "share" / "ai-hub" / "images"
IMAGE_TIMEOUT_S = 600
_GENERATED_IMG_MIN_PX = 300


def _is_generated_src(url: str) -> bool:
    # Matches OpenAI CDN patterns: oaiusercontent, oaistatic, oaidall, estuary, prod-files.oai*
    return any(p in url for p in ("oai", "estuary", "openai.com", "prod-files"))


def _estuary_srcs(page) -> set[str]:
    data = page.locator("img").evaluate_all(
        "imgs => imgs.map(i => [i.src, i.currentSrc || i.src])"
    )
    result: set[str] = set()
    for pair in data:
        for s in pair:
            if s and _is_generated_src(s):
                result.add(s)
    return result


def _click_first_available(page, selectors: tuple, timeout_ms: int = 10_000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).last
            loc.wait_for(state="visible", timeout=timeout_ms)
            loc.click()
            return True
        except Exception:
            continue
    return False


def _attach_reference_image(page, reference_path: Path) -> None:
    """Upload a reference image to the ChatGPT input before typing the prompt.

    Tries three strategies in order:
    1. Direct hidden file input (most reliable, bypasses UI changes).
    2. Sub-menu from attach button (ChatGPT classic layout).
    3. Direct file chooser intercept from attach button (fallback).
    """
    # Strategy 1: set files directly on the hidden input — bypasses all UI
    for sel in ('input[type="file"]', 'input[accept*="image"]'):
        try:
            fi = page.locator(sel).first
            fi.wait_for(state="attached", timeout=2_000)
            fi.set_input_files(str(reference_path))
            page.wait_for_timeout(2_000)
            log.info("Reference image attached via file input: %s", reference_path.name)
            return
        except Exception:
            continue

    attach_trigger_selectors = (
        "button[aria-label='Add files and more']",
        "button[aria-label='Attach files']",
        "button[aria-label*='attach' i]",
        "button[aria-label*='file' i]",
        "button[aria-label*='Add' i]",
        "[data-testid*='attach']",
    )
    upload_item_selectors = (
        "[role='menuitem']:has-text('computer')",
        "[role='menuitem']:has-text('Upload')",
        "[role='menuitem']:has-text('file')",
        "[role='option']:has-text('computer')",
        "li:has-text('Upload from computer')",
        "li:has-text('From computer')",
        "button:has-text('Upload from computer')",
        "button:has-text('From computer')",
        "button:has-text('Upload')",
        "div[role='menuitem']:has-text('computer')",
        "div[role='menuitem']:has-text('Upload')",
    )
    try:
        # Strategy 2: open attach menu, find "Upload from computer" sub-menu item
        if not _click_first_available(page, attach_trigger_selectors, timeout_ms=5_000):
            log.warning("Reference image attach button not found — skipping.")
            return
        page.wait_for_timeout(500)

        submenu_clicked = False
        for sel in upload_item_selectors:
            try:
                item = page.locator(sel).first
                item.wait_for(state="visible", timeout=3_000)
                with page.expect_file_chooser(timeout=6_000) as fc_info:
                    item.click()
                fc_info.value.set_files(str(reference_path))
                submenu_clicked = True
                break
            except Exception:
                continue

        if not submenu_clicked:
            # Strategy 3: re-click attach button with file chooser interceptor active
            try:
                with page.expect_file_chooser(timeout=8_000) as fc_info:
                    _click_first_available(page, attach_trigger_selectors, timeout_ms=5_000)
                fc_info.value.set_files(str(reference_path))
            except Exception as exc:
                log.warning("Reference image attach failed (all strategies): %s", exc)
                return

        page.wait_for_timeout(3_000)
        log.info("Reference image attached: %s", reference_path.name)
    except Exception as exc:
        log.warning("Reference image attach failed: %s", exc)


def _fill_and_send(page, full_prompt: str, reference_image_path: Path | None = None) -> None:
    if reference_image_path and reference_image_path.exists():
        _attach_reference_image(page, reference_image_path)

    selectors = ("textarea", "div[contenteditable='true']", "[role='textbox']")
    composer = None
    for sel in selectors:
        loc = page.locator(sel).last
        try:
            loc.wait_for(state="visible", timeout=10_000)
            # Focus the composer before filling so the send button / Enter key
            # bind to THIS GPT's conversation and don't drop the prompt (issue 002).
            try:
                loc.click()
            except Exception:
                pass
            loc.fill(full_prompt)
            composer = loc
            break
        except Exception:
            continue
    if composer is None:
        raise RuntimeError("Campo de texto do ChatGPT não encontrado.")

    # Small settle so ChatGPT enables the send button after the fill.
    page.wait_for_timeout(400)

    send_selectors = (
        "button[data-testid='send-button']",
        "button[aria-label*='Send']",
        "button[aria-label*='send']",
    )
    for sel in send_selectors:
        try:
            btn = page.locator(sel).last
            btn.wait_for(state="visible", timeout=5_000)
            btn.click()
            log.info("Prompt sent (%.60s…)", full_prompt)
            return
        except Exception:
            continue
    # Fallback: refocus the composer and press Enter so the keystroke lands there.
    try:
        composer.click()
    except Exception:
        pass
    page.keyboard.press("Enter")
    log.info("Prompt sent via Enter key (%.60s…)", full_prompt)


def _wait_for_done(page) -> None:
    stop_selectors = (
        "button[aria-label='Stop streaming']",
        "button[aria-label='Stop generating']",
        "[data-testid='stop-button']",
        "button[aria-label*='stop' i]",
    )

    def any_stop() -> bool:
        try:
            return any(page.locator(s).is_visible() for s in stop_selectors)
        except Exception:
            return False

    log.info("Waiting for generation to start (stop button, up to 60s)…")
    deadline = time.time() + 60
    while time.time() < deadline and not any_stop():
        page.wait_for_timeout(2_000)

    if not any_stop():
        # Generation never started. Fail fast with a clear error instead of
        # polling ~600s in _wait_for_new_image for an image that will never
        # appear (issue 002). A bare chatgpt.com/ (or chat.openai.com/) URL means
        # the GPT context was lost when the prompt was sent.
        cur = (page.url or "")
        base = cur.split("?")[0].rstrip("/")
        if base in ("https://chatgpt.com", "https://chat.openai.com"):
            raise RuntimeError(
                "generation_did_not_start: page reverted to ChatGPT home — "
                f"GPT context lost on send (url={cur!r})."
            )
        raise RuntimeError(
            f"generation_did_not_start: stop button never appeared (url={cur!r})."
        )

    log.info("Generation started (stop button visible). Waiting up to %ds…", IMAGE_TIMEOUT_S)

    last_log = time.time()
    deadline = time.time() + IMAGE_TIMEOUT_S
    while time.time() < deadline:
        if not any_stop():
            log.info("Image generation complete.")
            return
        if time.time() - last_log >= 30:
            elapsed = IMAGE_TIMEOUT_S - (deadline - time.time())
            log.info("Still generating… %.0fs elapsed of %ds timeout.", elapsed, IMAGE_TIMEOUT_S)
            last_log = time.time()
        page.wait_for_timeout(15_000)
    raise RuntimeError("ChatGPT não concluiu a geração dentro do tempo limite.")


def _wait_for_new_image(page, before_srcs: set[str]) -> str:
    log.info("_wait_for_new_image start — before_srcs count=%d", len(before_srcs))
    deadline = time.time() + IMAGE_TIMEOUT_S
    iteration = 0
    last_logged_srcs: set[str] = set()
    while time.time() < deadline:
        iteration += 1
        all_imgs = page.locator("img")
        try:
            count = all_imgs.count()
        except Exception as exc:
            log.warning("iter=%d img count failed: %s", iteration, exc)
            page.wait_for_timeout(15_000)
            continue

        log.info("iter=%d page_url=%.80s img_count=%d", iteration, page.url, count)

        for i in range(count - 1, -1, -1):
            try:
                img = all_imgs.nth(i)
                info = img.evaluate(
                    "img => ({src: img.src, cur: img.currentSrc || img.src, "
                    "w: img.naturalWidth, h: img.naturalHeight})"
                )
                src = info.get("src") or ""
                cur = info.get("cur") or ""
                w, h = info.get("w", 0), info.get("h", 0)
                key = src or cur
                if w < _GENERATED_IMG_MIN_PX or h < _GENERATED_IMG_MIN_PX:
                    continue
                if not (_is_generated_src(src) or _is_generated_src(cur)):
                    if key and key not in last_logged_srcs:
                        log.info("large-no-cdn %dx%d — %.120s", w, h, key)
                        last_logged_srcs.add(key)
                    continue
                if src in before_srcs or cur in before_srcs:
                    if key not in last_logged_srcs:
                        log.info("large-in-before %dx%d — %.120s", w, h, key)
                        last_logged_srcs.add(key)
                    continue
                log.info("candidate found %dx%d — %.120s", w, h, key)
                img.scroll_into_view_if_needed()
                img.click()
                page.wait_for_timeout(8_000)
                return src or cur
            except Exception as exc:
                log.warning("iter=%d img[%d] error: %s", iteration, i, exc)
                continue
        page.wait_for_timeout(15_000)
    raise RuntimeError("Nenhuma imagem nova apareceu dentro do tempo limite.")


def _download_image(page, src: str, output_path: Path) -> None:
    import urllib.parse
    parsed = urllib.parse.urlparse(src)
    params = urllib.parse.parse_qs(parsed.query)
    file_id = (params.get("id") or [""])[0]

    page.wait_for_timeout(3_000)
    best_src = src
    if file_id:
        candidates = page.locator("img").evaluate_all(
            "imgs => imgs.map(img => ({src: img.currentSrc || img.src, "
            "w: img.naturalWidth, h: img.naturalHeight}))"
        )
        best_area = 0
        for c in candidates:
            csrc = c.get("src") or ""
            if file_id not in csrc:
                continue
            w = int(c.get("w") or 0)
            h = int(c.get("h") or 0)
            if w * h > best_area:
                best_area = w * h
                best_src = csrc

    output_path.parent.mkdir(parents=True, exist_ok=True)
    response = page.context.request.get(best_src, timeout=120_000)
    if not response.ok:
        raise RuntimeError(f"Falha ao baixar imagem: HTTP {response.status} — {best_src}")

    raw = response.body()
    try:
        from PIL import Image as _PILImage
        img_obj = _PILImage.open(io.BytesIO(raw)).convert("RGBA")
        img_obj.save(str(output_path), format="PNG")
    except Exception:
        output_path.write_bytes(raw)


def delete_current_chat(page) -> bool:
    """Best-effort delete of the currently-open ChatGPT conversation.

    Opens the conversation options menu, clicks Delete, and confirms. The
    ChatGPT UI changes often, so every step is best-effort: on any failure we
    log and return False WITHOUT raising, so a delete never sinks a successful
    generation (issue 003).
    """
    menu_selectors = (
        "button[data-testid='conversation-options-button']",
        "button[aria-label*='options' i]",
        "button[aria-label*='opções' i]",
        "[data-testid='conversation-turn'] button[aria-label*='More' i]",
        "button[aria-label*='More' i]",
    )
    delete_item_selectors = (
        "[role='menuitem']:has-text('Delete')",
        "[role='menuitem']:has-text('Excluir')",
        "[role='menuitem']:has-text('Apagar')",
        "div[role='menuitem']:has-text('Delete')",
        "button:has-text('Delete chat')",
    )
    confirm_selectors = (
        "button[data-testid='delete-conversation-confirm-button']",
        "button:has-text('Delete')",
        "button:has-text('Excluir')",
        "button:has-text('Confirm')",
        "[role='dialog'] button:has-text('Delete')",
    )
    try:
        if not _click_first_available(page, menu_selectors, timeout_ms=5_000):
            log.warning("delete_current_chat: options menu not found — skipping.")
            return False
        page.wait_for_timeout(500)
        if not _click_first_available(page, delete_item_selectors, timeout_ms=4_000):
            log.warning("delete_current_chat: Delete item not found — skipping.")
            return False
        page.wait_for_timeout(500)
        if not _click_first_available(page, confirm_selectors, timeout_ms=4_000):
            log.warning("delete_current_chat: confirm button not found — skipping.")
            return False
        page.wait_for_timeout(1_500)
        log.info("delete_current_chat: conversation deleted.")
        return True
    except Exception as exc:
        log.warning("delete_current_chat failed (best-effort): %s", exc)
        return False


def generate_image(
    gpt_url: str,
    prompt: str,
    orientation: str = "portrait",
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    greeting: str = "Hey, ",
    cdp_url: str = "http://127.0.0.1:9222",
    reference_image_path: Path | None = None,
    delete_chat: bool = False,
) -> Path:
    """Sends prompt to a ChatGPT image GPT and saves the generated image.

    Uses the shared Chrome instance — must be already running.
    If reference_image_path is provided, the file is attached before the prompt.
    """
    from chrome_manager import ChromeManager

    full_prompt = f"{greeting}{prompt} — orientação {orientation}"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = Path(output_dir)
    output_path = output_dir / f"ai-hub-{stamp}.png"

    log.info("Generating image: %.80s", full_prompt)

    with ChromeManager(cdp_url=cdp_url) as mgr:
        # Close stale chatgpt pages to avoid accumulation (URL changes after navigate).
        ctx = mgr.context
        for p in list(ctx.pages):
            try:
                if "chatgpt.com" in p.url:
                    p.close()
            except Exception:
                pass

        # Always navigate fresh to the GPT URL so the correct GPT context loads.
        page = ctx.new_page()
        page.goto(gpt_url, wait_until="domcontentloaded", timeout=60_000)
        # Wait for the input box to be ready (GPT fully loaded).
        for sel in ("textarea", "div[contenteditable='true']", "[role='textbox']"):
            try:
                page.locator(sel).last.wait_for(state="visible", timeout=20_000)
                break
            except Exception:
                continue
        log.info("GPT page loaded: %s", page.url)

        before_srcs = _estuary_srcs(page)
        log.info("Existing images in page: %d", len(before_srcs))

        _fill_and_send(page, full_prompt, reference_image_path=reference_image_path)
        _wait_for_done(page)

        new_src = _wait_for_new_image(page, before_srcs)
        _download_image(page, new_src, output_path)

        # Best-effort: remove the chat from history after the image is safely
        # downloaded, so tests/automation don't leave orphan conversations (issue 003).
        if delete_chat:
            delete_current_chat(page)

    log.info("Image saved: %s", output_path)
    return output_path
