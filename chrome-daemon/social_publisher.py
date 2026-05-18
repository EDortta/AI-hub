"""Social media publishing via the shared Chrome instance.

Extracted from Characters/Dopamin Captain/daily_post.py.
Uses ChromeManager so Chrome is never launched directly by the caller.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("ai-hub.social")

CDP_URL_DEFAULT = "http://127.0.0.1:9222"


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

        add_media_btn = page.locator("button[aria-label='Add media'], button[aria-label='Adicionar mídia']").first
        add_media_btn.wait_for(state="visible", timeout=30_000)

        with page.expect_file_chooser(timeout=15_000) as fc_info:
            add_media_btn.click()
        fc_info.value.set_files(str(image_path))
        page.wait_for_timeout(6_000)

        done_selectors = (
            "button[aria-label='Done']", "button[aria-label='Save']",
            "button[aria-label='Concluído']", "button[aria-label='Salvar']",
            "button:has-text('Done')", "button:has-text('Save')",
            "button:has-text('Apply')", "button:has-text('Next')",
            "button:has-text('Confirm')", "button:has-text('Concluído')",
            "button:has-text('Salvar')", "button:has-text('Avançar')",
            "button.share-creation-state__done", "button[data-testid='done-button']",
        )
        if _click_first_available(page, done_selectors, timeout_ms=10_000):
            page.wait_for_timeout(3_000)

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
