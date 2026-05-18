"""Chrome lifecycle manager for AI-hub.

Owns the single Chrome instance shared by all registered projects.
Chrome runs on Xvfb :99 (hidden). Call show()/hide() to expose it temporarily.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
XVFB_DISPLAY = ":99"
CHROME_PROFILE = Path.home() / ".local" / "share" / "ai-hub" / "chrome-profile"

_SINGLETON_FILES = ("SingletonCookie", "SingletonLock", "SingletonSocket")


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def is_cdp_available(cdp_url: str = CDP_URL) -> bool:
    try:
        with urllib.request.urlopen(f"{cdp_url}/json", timeout=2):
            return True
    except Exception:
        return False


def _chrome_executable() -> str:
    chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    if not chrome:
        raise RuntimeError("Google Chrome não encontrado. Instale o google-chrome-stable.")
    return chrome


def _singleton_pid(profile_dir: Path) -> int | None:
    sl = profile_dir / "SingletonLock"
    if not sl.exists():
        return None
    try:
        target = os.readlink(sl)
        return int(target.rsplit("-", 1)[-1])
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def remove_singleton_files(profile_dir: Path) -> None:
    pid = _singleton_pid(profile_dir)
    if pid is not None and _pid_alive(pid):
        raise RuntimeError(
            f"Chrome com PID {pid} já está usando este perfil. "
            "Feche-o antes de iniciar o ai-hub daemon."
        )
    for name in _SINGLETON_FILES:
        with contextlib.suppress(FileNotFoundError):
            (profile_dir / name).unlink()


# ---------------------------------------------------------------------------
# Xvfb
# ---------------------------------------------------------------------------

def ensure_xvfb(display: str = XVFB_DISPLAY) -> str:
    """Garante display virtual Xvfb. Retorna display a usar, ou '' para headless nativo."""
    if not shutil.which("Xvfb"):
        print("[chrome] Xvfb não encontrado — Chrome vai rodar em modo headless nativo.")
        return ""
    try:
        r = subprocess.run(
            ["xdpyinfo", "-display", display],
            capture_output=True, timeout=2,
        )
        if r.returncode == 0:
            return display
    except Exception:
        pass
    subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "1280x1024x24"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(1.5)
    print(f"[chrome] Xvfb iniciado em {display}.")
    return display


# ---------------------------------------------------------------------------
# Chrome launch
# ---------------------------------------------------------------------------

def launch_chrome(
    profile_dir: Path = CHROME_PROFILE,
    display: str = XVFB_DISPLAY,
    cdp_url: str = CDP_URL,
) -> None:
    """Lança Chrome se CDP ainda não estiver disponível.

    display='' → headless nativo (sem display virtual).
    display=':99' etc → Xvfb/X11.
    """
    if is_cdp_available(cdp_url):
        return

    profile_dir.mkdir(parents=True, exist_ok=True)
    remove_singleton_files(profile_dir)

    port = int(cdp_url.rstrip("/").rsplit(":", 1)[-1])
    chrome = _chrome_executable()

    base_args = [
        chrome,
        f"--user-data-dir={profile_dir}",
        "--profile-directory=Default",
        "--no-first-run",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--window-size=1280,1024",
        f"--remote-debugging-port={port}",
    ]

    env = os.environ.copy()

    if not display:
        # Headless nativo — não precisa de display virtual
        args = base_args + ["--headless=new"]
        env.pop("DISPLAY", None)
        print(f"[chrome] Chrome headless — aguardando CDP em {cdp_url}...")
    else:
        args = base_args + ["--new-window"]
        env["DISPLAY"] = display
        print(f"[chrome] Chrome em display={display} — aguardando CDP em {cdp_url}...")

    subprocess.Popen(
        args,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )

    deadline = time.time() + 30
    while time.time() < deadline:
        time.sleep(1)
        if is_cdp_available(cdp_url):
            print("[chrome] CDP pronto.")
            return
    raise RuntimeError(f"Chrome não expôs CDP em {cdp_url} dentro de 30s.")


def launch_visible_chrome(
    profile_dir: Path = CHROME_PROFILE,
    cdp_url: str = CDP_URL,
) -> None:
    """Lança Chrome no display real (para login manual)."""
    real_display = os.environ.get("DISPLAY", ":0")
    launch_chrome(profile_dir=profile_dir, display=real_display, cdp_url=cdp_url)


# ---------------------------------------------------------------------------
# Playwright connection
# ---------------------------------------------------------------------------

class ChromeManager:
    """Context manager que conecta ao Chrome via Playwright CDP."""

    def __init__(self, cdp_url: str = CDP_URL):
        self._cdp_url = cdp_url
        self._playwright = None
        self._browser = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._playwright = sync_playwright().__enter__()
        self._browser = self._playwright.chromium.connect_over_cdp(self._cdp_url)
        return self

    def __exit__(self, *_):
        with contextlib.suppress(Exception):
            self._playwright.__exit__(None, None, None)

    @property
    def context(self):
        ctxs = self._browser.contexts
        return ctxs[0] if ctxs else self._browser.new_context()

    def find_page(self, url_contains: str):
        for page in self.context.pages:
            try:
                if url_contains in (page.url or ""):
                    return page
            except Exception:
                continue
        return None

    def get_or_open_page(self, url: str):
        """Returns existing page for url or opens a new one."""
        base = url.split("?")[0]
        for page in self.context.pages:
            try:
                if (page.url or "").split("?")[0] == base:
                    return page
            except Exception:
                continue
        page = self.context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        return page

    def send_message(self, url: str, text: str) -> bool:
        """Navigate to url, type text into the ChatGPT input and send it."""
        page = self.get_or_open_page(url)
        try:
            selectors = ("textarea", "div[contenteditable='true']", "[role='textbox']")
            for sel in selectors:
                loc = page.locator(sel).last
                try:
                    loc.wait_for(state="visible", timeout=5_000)
                    loc.click()
                    page.keyboard.type(text)
                    break
                except Exception:
                    continue
            else:
                return False

            send_sels = (
                "button[data-testid='send-button']",
                "button[aria-label*='Send']",
                "button[aria-label*='send']",
            )
            for ss in send_sels:
                try:
                    page.locator(ss).wait_for(state="visible", timeout=3_000)
                    page.locator(ss).click()
                    return True
                except Exception:
                    pass
            page.keyboard.press("Enter")
            return True
        except Exception as e:
            import logging
            logging.getLogger("ai-hub.chrome").warning("send_message error: %s", e)
            return False
