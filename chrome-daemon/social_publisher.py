"""Social media publishing via the shared Chrome instance.

Extracted from Characters/Dopamin Captain/daily_post.py.
Uses ChromeManager so Chrome is never launched directly by the caller.
"""
from __future__ import annotations

import contextlib
import logging
import re
import time
from pathlib import Path

log = logging.getLogger("ai-hub.social")

CDP_URL_DEFAULT = "http://127.0.0.1:9222"

NOTE_MAX_CHARS = 300  # LinkedIn's own cap on connection-invite notes.


def _click_first_available(page, selectors: tuple, timeout_ms: int = 10_000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout_ms)
            loc.click()
            return True
        except Exception:
            continue
    return False


def _is_visible(page, selector: str) -> bool:
    try:
        return page.locator(selector).first.is_visible()
    except Exception:
        return False


class LinkedInChallenge(RuntimeError):
    """LinkedIn showed a checkpoint/captcha/unusual-activity challenge.

    Callers must not retry automatically on this — screenshot_path is the
    evidence a human needs to look at before anything else runs.
    """

    def __init__(self, message: str, screenshot_path: str):
        super().__init__(message)
        self.screenshot_path = screenshot_path


_CHALLENGE_TEXT_RE = re.compile(r"unusual activity|atividade incomum", re.IGNORECASE)

_ACTIONS_DIR = Path.home() / ".local" / "share" / "ai-hub" / "linkedin-actions"


def _detect_challenge(page) -> str | None:
    url = page.url or ""
    if "/checkpoint" in url or "/authwall" in url:
        return f"checkpoint/authwall URL: {url}"
    try:
        if page.locator(
            "iframe[src*='captcha' i], iframe[title*='captcha' i], iframe[src*='arkose' i]"
        ).count() > 0:
            return "captcha/challenge iframe present"
    except Exception:
        pass
    try:
        if page.get_by_text(_CHALLENGE_TEXT_RE).count() > 0:
            return "'unusual activity' text present"
    except Exception:
        pass
    return None


def _save_screenshot(page, tag: str) -> str:
    _ACTIONS_DIR.mkdir(parents=True, exist_ok=True)
    out = _ACTIONS_DIR / f"{tag}-{int(time.time())}.png"
    try:
        page.screenshot(path=str(out))
    except Exception:
        log.warning("Could not save screenshot to %s", out)
    return str(out)


def _fail_on_challenge(page, tag: str) -> None:
    """Stop and raise if a checkpoint/captcha/challenge is on screen — never retried."""
    reason = _detect_challenge(page)
    if reason:
        shot = _save_screenshot(page, f"{tag}-challenge")
        log.error("LinkedIn challenge detected (%s): %s — screenshot=%s", tag, reason, shot)
        raise LinkedInChallenge(f"LinkedIn challenge detected: {reason}", shot)


def _goto_profile_or_page(page, url: str, timeout_ms: int = 60_000) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass
    page.wait_for_timeout(1_000)
    if "linkedin.com/login" in page.url or "linkedin.com/authwall" in page.url:
        raise RuntimeError(
            "LinkedIn session not found — run 'ai-hub setup' to log in manually."
        )


def publish_to_x(
    image_path: Path,
    caption: str,
    x_compose_url: str = "https://x.com/compose/post",
    cdp_url: str = CDP_URL_DEFAULT,
) -> None:
    """Post an image + caption to X (Twitter) via the shared Chrome."""
    from chrome_manager import ChromeManager

    log.info("Publishing to X: %.60s", caption)

    with ChromeManager(cdp_url=cdp_url) as mgr:
        page = mgr.get_or_open_page(x_compose_url)

        # X uses React/Draft.js contenteditable — must click then type (fill() bypasses React).
        compose_selectors = (
            "[aria-label='Post text']",
            "[role='textbox']",
            "div[contenteditable='true']",
            "textarea",
        )
        typed = False
        for selector in compose_selectors:
            locator = page.locator(selector).first
            try:
                locator.wait_for(state="visible", timeout=30_000)
                locator.click()
                page.keyboard.type(caption)
                typed = True
                break
            except Exception:
                continue
        if not typed:
            raise RuntimeError("Could not find X compose textbox.")

        file_inputs = page.locator("input[type='file']")
        if file_inputs.count() == 0:
            raise RuntimeError("Could not find X media upload input.")
        file_inputs.first.set_input_files(str(image_path))

        page.wait_for_timeout(3_000)
        post_selectors = (
            "button[data-testid='tweetButton']",
            "button[data-testid='tweetButtonInline']",
            "button:has-text('Post')",
        )
        if not _click_first_available(page, post_selectors, timeout_ms=30_000):
            raise RuntimeError("Could not find X Post button.")

        page.wait_for_timeout(5_000)

    log.info("X post published.")


def publish_to_linkedin(
    image_path: Path,
    caption: str,
    linkedin_url: str = "https://www.linkedin.com/feed/",
    cdp_url: str = CDP_URL_DEFAULT,
) -> None:
    """Post an image + caption to LinkedIn via the shared Chrome."""
    from chrome_manager import ChromeManager

    log.info("Publishing to LinkedIn: %.60s", caption)

    with ChromeManager(cdp_url=cdp_url) as mgr:
        # Find an existing LinkedIn tab or navigate to the feed.
        page = None
        for p in mgr.context.pages:
            if "linkedin.com" in (p.url or ""):
                page = p
                break
        if page is None:
            page = mgr.get_or_open_page(linkedin_url)

        page.bring_to_front()
        page.wait_for_timeout(2_000)

        if "linkedin.com/feed" not in page.url:
            page.goto(linkedin_url, wait_until="domcontentloaded", timeout=120_000)
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass

        if "linkedin.com/login" in page.url or "linkedin.com/authwall" in page.url:
            raise RuntimeError(
                "LinkedIn session not found — run 'ai-hub setup' to log in manually."
            )

        # Dismiss stale compose modal if left open.
        if page.locator("button[aria-label='Add media'], button[aria-label='Adicionar mídia']").is_visible():
            log.debug("LinkedIn compose modal already open — reusing.")
        else:
            if page.locator("[data-testid='interop-shadowdom']").is_visible():
                page.keyboard.press("Escape")
                page.wait_for_timeout(1_000)

            start_post_selectors = (
                ':text-is("Start a post")',
                ':text-is("Começar uma publicação")',
                ':text-is("Compartilhar uma publicação")',
                '[placeholder*="post"]',
                '[placeholder*="publicação"]',
            )
            if not _click_first_available(page, start_post_selectors, timeout_ms=5_000):
                raise RuntimeError("Could not find LinkedIn 'Start a post' button.")

        # Try to attach via hidden file input first (most reliable)
        file_inputs = page.locator("input[type='file']")
        used_file_input = False
        if file_inputs.count() > 0:
            try:
                file_inputs.first.set_input_files(str(image_path))
                used_file_input = True
                log.info("LinkedIn image attached via direct file input.")
            except Exception:
                pass

        if not used_file_input:
            add_media_selectors = (
                "button[aria-label='Add media']",
                "button[aria-label='Adicionar mídia']",
                "button[aria-label='Add photo']",
                "button[aria-label='Adicionar foto']",
                "button[aria-label='Media']",
                "button[aria-label='Mídia']",
                "button[aria-label='Add a photo']",
                "button[aria-label*='media' i]",
                "button[aria-label*='photo' i]",
                "button[aria-label*='mídia' i]",
                "button[aria-label*='foto' i]",
                "li-icon[type='image-medium']",
            )
            add_media_btn = None
            for sel in add_media_selectors:
                try:
                    loc = page.locator(sel).first
                    loc.wait_for(state="visible", timeout=5_000)
                    add_media_btn = loc
                    break
                except Exception:
                    continue

            if add_media_btn is None:
                raise RuntimeError(
                    "Could not find LinkedIn Add media button. "
                    "Selectors tried: " + ", ".join(add_media_selectors)
                )

            try:
                with page.expect_file_chooser(timeout=15_000) as fc_info:
                    add_media_btn.click()
                fc_info.value.set_files(str(image_path))
            except Exception:
                # file chooser didn't appear — try clicking and setting via input
                add_media_btn.click()
                page.wait_for_timeout(2_000)
                fi = page.locator("input[type='file']")
                if fi.count() > 0:
                    fi.first.set_input_files(str(image_path))
                else:
                    raise RuntimeError("LinkedIn media attach failed: file chooser timed out and no file input found.")
        page.wait_for_timeout(6_000)

        # LinkedIn image editor may have multiple Next/Done steps — advance through all of them.
        advance_selectors = (
            "button:has-text('Next')", "button:has-text('Avançar')",
            "button:has-text('Done')", "button:has-text('Concluído')",
            "button:has-text('Save')", "button:has-text('Salvar')",
            "button:has-text('Apply')", "button:has-text('Confirm')",
            "button[aria-label='Done']", "button[aria-label='Save']",
            "button[aria-label='Concluído']", "button[aria-label='Salvar']",
            "button.share-creation-state__done", "button[data-testid='done-button']",
        )
        for _ in range(4):  # advance through up to 4 editor steps
            if _click_first_available(page, advance_selectors, timeout_ms=3_000):
                page.wait_for_timeout(2_000)
            else:
                break

        typed = False
        editor_selectors = (
            "pierce/div[contenteditable='true']",
            "div[contenteditable='true']",
            "div[role='textbox']",
            "[role='textbox']",
        )
        for selector in editor_selectors:
            try:
                locator = page.locator(selector).first
                locator.wait_for(state="visible", timeout=5_000)
                locator.click()
                page.keyboard.type(caption)
                typed = True
                break
            except Exception:
                continue

        if not typed:
            for modal_selector in ("div[role='dialog']", ".share-box-v2", "[data-test-modal]"):
                try:
                    modal = page.locator(modal_selector).first
                    modal.wait_for(state="visible", timeout=5_000)
                    box = modal.bounding_box()
                    if box:
                        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + 80)
                        page.keyboard.type(caption)
                        typed = True
                        break
                except Exception:
                    continue

        if not typed:
            raise RuntimeError("Could not find LinkedIn post editor.")

        page.wait_for_timeout(1_000)

        post_selectors = (
            "button.share-actions__primary-action",
            "button[aria-label='Post']",
            "button[aria-label='Publicar']",
            "button:has-text('Post')",
            "button:has-text('Publicar')",
        )
        if not _click_first_available(page, post_selectors, timeout_ms=30_000):
            raise RuntimeError("Could not find LinkedIn Post button.")

        page.wait_for_timeout(5_000)

    log.info("LinkedIn post published.")


def follow_page(url: str, cdp_url: str = CDP_URL_DEFAULT) -> dict:
    """Follow a LinkedIn company or person page — same 'Follow'/'Seguir' button either way.

    Idempotent: if already following, returns already_following=True without
    clicking again. Self-contained: opens its own tab and always closes it in
    a finally block, win or lose (issue 004 — no tab may survive between
    calls on this host).
    """
    from chrome_manager import ChromeManager

    log.info("LinkedIn follow: %s", url)

    with ChromeManager(cdp_url=cdp_url) as mgr:
        page = mgr.context.new_page()
        try:
            _goto_profile_or_page(page, url)
            _fail_on_challenge(page, "follow")

            already_following = any(
                _is_visible(page, sel)
                for sel in ("button:has-text('Following')", "button:has-text('Seguindo')")
            )

            if not already_following:
                follow_selectors = (
                    "button[aria-label^='Follow ']",
                    "button[aria-label^='Seguir ']",
                    "button:has-text('Follow')",
                    "button:has-text('Seguir')",
                )
                if not _click_first_available(page, follow_selectors, timeout_ms=15_000):
                    raise RuntimeError("Could not find LinkedIn Follow button.")
                page.wait_for_timeout(2_000)
                _fail_on_challenge(page, "follow-post-click")

            shot = _save_screenshot(page, "follow")
            log.info("LinkedIn follow done (already_following=%s): %s", already_following, url)
            return {"ok": True, "already_following": already_following, "screenshot_path": shot}
        finally:
            with contextlib.suppress(Exception):
                page.close()


def connect_with_note(profile_url: str, note: str, cdp_url: str = CDP_URL_DEFAULT) -> dict:
    """Send a LinkedIn connection invite with a note (~300-char LinkedIn cap).

    Stays 'Pending' until the other side accepts — this only sends the
    invite. Self-contained: own tab, closed in finally regardless of outcome.
    """
    from chrome_manager import ChromeManager

    if len(note) > NOTE_MAX_CHARS:
        raise ValueError(f"note exceeds LinkedIn's ~{NOTE_MAX_CHARS}-char limit ({len(note)} chars)")

    log.info("LinkedIn connect: %s", profile_url)

    with ChromeManager(cdp_url=cdp_url) as mgr:
        page = mgr.context.new_page()
        try:
            _goto_profile_or_page(page, profile_url)
            _fail_on_challenge(page, "connect")

            already_pending = any(
                _is_visible(page, sel)
                for sel in ("button:has-text('Pending')", "button:has-text('Pendente')")
            )
            if already_pending:
                shot = _save_screenshot(page, "connect")
                log.info("LinkedIn connect already pending: %s", profile_url)
                return {"ok": True, "already_pending": True, "screenshot_path": shot}

            connect_selectors = (
                "button[aria-label^='Invite']",
                "button[aria-label^='Convidar']",
                "button:has-text('Connect')",
                "button:has-text('Conectar')",
            )
            if not _click_first_available(page, connect_selectors, timeout_ms=15_000):
                # On some profile layouts Connect is tucked behind a "More" overflow menu.
                more_selectors = (
                    "button[aria-label='More actions']",
                    "button[aria-label='Mais ações']",
                    "button:has-text('More')",
                    "button:has-text('Mais')",
                )
                if _click_first_available(page, more_selectors, timeout_ms=5_000):
                    page.wait_for_timeout(1_000)
                if not _click_first_available(page, connect_selectors, timeout_ms=5_000):
                    raise RuntimeError("Could not find LinkedIn Connect button.")

            page.wait_for_timeout(1_000)
            _fail_on_challenge(page, "connect-post-click")

            add_note_selectors = (
                "button[aria-label='Add a note']",
                "button[aria-label='Adicionar nota']",
                "button:has-text('Add a note')",
                "button:has-text('Adicionar nota')",
            )
            if not _click_first_available(page, add_note_selectors, timeout_ms=10_000):
                raise RuntimeError(
                    "Could not find LinkedIn 'Add a note' button — invite modal may have changed shape."
                )

            note_field_selectors = ("textarea[name='message']", "textarea#custom-message", "textarea")
            typed = False
            for sel in note_field_selectors:
                try:
                    loc = page.locator(sel).first
                    loc.wait_for(state="visible", timeout=5_000)
                    loc.fill(note)
                    typed = True
                    break
                except Exception:
                    continue
            if not typed:
                raise RuntimeError("Could not find LinkedIn note textarea.")

            send_selectors = (
                "button[aria-label='Send invitation']",
                "button[aria-label='Enviar convite']",
                "button:has-text('Send')",
                "button:has-text('Enviar')",
            )
            if not _click_first_available(page, send_selectors, timeout_ms=10_000):
                raise RuntimeError("Could not find LinkedIn 'Send invitation' button.")

            page.wait_for_timeout(2_000)
            _fail_on_challenge(page, "connect-post-send")

            shot = _save_screenshot(page, "connect")
            log.info("LinkedIn connect invite sent: %s", profile_url)
            return {"ok": True, "already_pending": False, "screenshot_path": shot}
        finally:
            with contextlib.suppress(Exception):
                page.close()


def send_message(profile_url: str, text: str, cdp_url: str = CDP_URL_DEFAULT) -> dict:
    """Send a LinkedIn message — requires an already-accepted 1st-degree connection.

    LinkedIn hides the 'Message' button for anyone who isn't 1st-degree
    (short of paid InMail, which this does not use). If the button is
    missing this fails explicitly instead of trying to work around it.
    Self-contained: own tab, closed in finally regardless of outcome.
    """
    from chrome_manager import ChromeManager

    log.info("LinkedIn message: %s", profile_url)

    with ChromeManager(cdp_url=cdp_url) as mgr:
        page = mgr.context.new_page()
        try:
            _goto_profile_or_page(page, profile_url)
            _fail_on_challenge(page, "message")

            message_selectors = (
                "button[aria-label^='Message']",
                "button[aria-label^='Mensagem']",
                "a[aria-label^='Message']",
                "main button:has-text('Message')",
                "main button:has-text('Mensagem')",
            )
            if not _click_first_available(page, message_selectors, timeout_ms=15_000):
                raise RuntimeError(
                    "No LinkedIn 'Message' button on this profile — not a 1st-degree "
                    "connection yet (or InMail-only), refusing to proceed."
                )

            page.wait_for_timeout(2_000)
            _fail_on_challenge(page, "message-post-click")

            compose_selectors = (
                "div.msg-form__contenteditable[contenteditable='true']",
                "div[contenteditable='true']",
                "div[role='textbox']",
            )
            typed = False
            for sel in compose_selectors:
                try:
                    loc = page.locator(sel).last
                    loc.wait_for(state="visible", timeout=10_000)
                    loc.click()
                    page.keyboard.type(text)
                    typed = True
                    break
                except Exception:
                    continue
            if not typed:
                raise RuntimeError("Could not find LinkedIn message compose box.")

            send_selectors = (
                "button.msg-form__send-button",
                "button[type='submit']:has-text('Send')",
                "button:has-text('Send')",
                "button:has-text('Enviar')",
            )
            if not _click_first_available(page, send_selectors, timeout_ms=10_000):
                raise RuntimeError("Could not find LinkedIn message Send button.")

            page.wait_for_timeout(2_000)
            _fail_on_challenge(page, "message-post-send")

            shot = _save_screenshot(page, "message")
            log.info("LinkedIn message sent: %s", profile_url)
            return {"ok": True, "screenshot_path": shot}
        finally:
            with contextlib.suppress(Exception):
                page.close()
