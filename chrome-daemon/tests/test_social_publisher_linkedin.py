"""Issue 004 — LinkedIn outreach (follow/connect/message) safety mechanics.

Covers the pieces that do not need a live Chrome/CDP endpoint: challenge
detection (must never be worked around or retried), the connect-note length
cap, and the click-first-available helper. The three public functions
(follow_page/connect_with_note/send_message) drive a real Playwright page and
are exercised manually against the real daemon — not unit-testable without a
live Chrome, same as publish_to_x/publish_to_linkedin before them.
"""
from __future__ import annotations

import pytest

import social_publisher as sp


class _FakeLocator:
    def __init__(self, *, count: int = 0, visible: bool = False, raise_on_wait: bool = False):
        self._count = count
        self._visible = visible
        self._raise_on_wait = raise_on_wait
        self.clicked = False

    @property
    def first(self):
        return self

    @property
    def last(self):
        return self

    def count(self):
        return self._count

    def is_visible(self):
        return self._visible

    def wait_for(self, state="visible", timeout=10_000):
        if self._raise_on_wait:
            raise TimeoutError("not visible")

    def click(self):
        self.clicked = True

    def fill(self, text):
        pass


class _FakePage:
    def __init__(self, url: str = "https://www.linkedin.com/in/someone/", locators: dict | None = None,
                 text_matches: int = 0):
        self.url = url
        self._locators = locators or {}
        self._text_matches = text_matches
        self.screenshots = []

    def locator(self, selector: str):
        return self._locators.get(selector, _FakeLocator(count=0, raise_on_wait=True))

    def get_by_text(self, pattern):
        return _FakeLocator(count=self._text_matches)

    def screenshot(self, path: str):
        self.screenshots.append(path)


# --- challenge detection -----------------------------------------------------

def test_detect_challenge_clean_page_returns_none():
    page = _FakePage(url="https://www.linkedin.com/in/someone/")
    assert sp._detect_challenge(page) is None


def test_detect_challenge_checkpoint_url():
    page = _FakePage(url="https://www.linkedin.com/checkpoint/challenge")
    assert "checkpoint" in sp._detect_challenge(page)


def test_detect_challenge_authwall_url():
    page = _FakePage(url="https://www.linkedin.com/authwall?x=1")
    assert "authwall" in sp._detect_challenge(page)


def test_detect_challenge_captcha_iframe():
    page = _FakePage(locators={
        "iframe[src*='captcha' i], iframe[title*='captcha' i], iframe[src*='arkose' i]":
            _FakeLocator(count=1),
    })
    assert "captcha" in sp._detect_challenge(page)


def test_detect_challenge_unusual_activity_text():
    page = _FakePage(text_matches=1)
    assert "unusual activity" in sp._detect_challenge(page)


def test_fail_on_challenge_raises_and_screenshots(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "_ACTIONS_DIR", tmp_path)
    page = _FakePage(url="https://www.linkedin.com/checkpoint/challenge")
    with pytest.raises(sp.LinkedInChallenge) as exc_info:
        sp._fail_on_challenge(page, "follow")
    assert exc_info.value.screenshot_path
    assert page.screenshots, "a challenge must leave evidence behind, not just an error message"


def test_fail_on_challenge_noop_on_clean_page(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "_ACTIONS_DIR", tmp_path)
    page = _FakePage(url="https://www.linkedin.com/in/someone/")
    sp._fail_on_challenge(page, "follow")  # must not raise
    assert page.screenshots == []


# --- goto + session check ----------------------------------------------------

def test_goto_profile_or_page_rejects_login_redirect():
    class _RedirectingPage(_FakePage):
        def goto(self, url, wait_until, timeout):
            self.url = "https://www.linkedin.com/login"

        def wait_for_load_state(self, state, timeout):
            pass

        def wait_for_timeout(self, ms):
            pass

    page = _RedirectingPage()
    with pytest.raises(RuntimeError, match="session not found"):
        sp._goto_profile_or_page(page, "https://www.linkedin.com/in/someone/")


# --- click_first_available ---------------------------------------------------

def test_click_first_available_clicks_the_first_visible_selector():
    hit = _FakeLocator(count=1)
    page = _FakePage(locators={"a": _FakeLocator(raise_on_wait=True), "b": hit})
    assert sp._click_first_available(page, ("a", "b")) is True
    assert hit.clicked is True


def test_click_first_available_returns_false_when_nothing_matches():
    page = _FakePage(locators={"a": _FakeLocator(raise_on_wait=True)})
    assert sp._click_first_available(page, ("a",)) is False


# --- connect note length cap --------------------------------------------------

def test_connect_with_note_rejects_note_over_linkedins_cap():
    with pytest.raises(ValueError, match="300"):
        sp.connect_with_note("https://www.linkedin.com/in/someone/", "x" * 301)
