"""Chrome lifecycle manager for AI-hub.

Owns the single Chrome instance shared by all registered projects.
Chrome runs on Xvfb :99 (hidden). Call show()/hide() to expose it temporarily.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import os
import shutil
import signal
import subprocess
import time
import urllib.request
from pathlib import Path

CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
XVFB_DISPLAY = ":99"
CHROME_PROFILE = Path.home() / ".local" / "share" / "ai-hub" / "chrome-profile"

_SINGLETON_FILES = ("SingletonCookie", "SingletonLock", "SingletonSocket")

# Tracked Chrome subprocess (set by launch_chrome).
_chrome_process: subprocess.Popen | None = None

# Session cache: avoids a Playwright round-trip before every operation.
_session_cache: dict = {"ok": None, "checked_at": 0.0}
_SESSION_CACHE_TTL = 300  # 5 minutes

# Executor dedicado para chamadas Playwright (sync API).
# max_workers=8 suporta até 8 watchers simultâneos sem serializar.
# Cada thread limpa o running-loop no finally de _run_playwright_fn — veja abaixo.
playwright_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=8,
    thread_name_prefix="playwright",
)


async def run_playwright_async(fn, timeout: int = 700):
    """Run a sync playwright callable in a thread from the global executor.

    Playwright 1.x calls asyncio._set_running_loop() during sync operation,
    leaving stale running-loop state on the thread. We clear it in a finally
    block so the reused thread is clean for the next call, avoiding the
    'Sync API inside asyncio loop' error without spawning a new process each time.
    """
    loop = asyncio.get_event_loop()

    def _wrapped():
        try:
            return fn()
        finally:
            # Clear stale running-loop state set by Playwright's sync API.
            # asyncio._set_running_loop is private but stable since Python 3.7
            # and is the exact mechanism Playwright uses — set_event_loop() alone
            # does not fix this.
            try:
                asyncio._set_running_loop(None)  # type: ignore[attr-defined]
            except Exception:
                pass

    return await loop.run_in_executor(playwright_executor, _wrapped)


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
        # Anti-detection: hides automation traces that trigger Cloudflare Turnstile
        "--disable-blink-features=AutomationControlled",
        "--exclude-switches=enable-automation",
        "--disable-automation",
        # Keep the on-device "optimization guide" LLM (Gemini Nano, ~2.7 GB) out
        # of this automation profile: it never benefits a headless ChatGPT bridge
        # and its background compute destabilized the CDP connection (high CPU →
        # watchdog kills → send/last-message timeouts). Disable the features and
        # stop the component downloader from refetching the model.
        "--disable-features=OptimizationGuideOnDeviceModel,OptimizationGuideModelDownloading,"
        "OptimizationHints,TextSafetyClassifier,OnDeviceHeadSuggest",
        "--disable-component-update",
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

    global _chrome_process
    _chrome_process = subprocess.Popen(
        args,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )

    deadline = time.time() + 60
    while time.time() < deadline:
        time.sleep(1)
        if is_cdp_available(cdp_url):
            print("[chrome] CDP pronto.")
            return
    raise RuntimeError(f"Chrome não expôs CDP em {cdp_url} dentro de 60s.")


def launch_visible_chrome(
    profile_dir: Path = CHROME_PROFILE,
    cdp_url: str = CDP_URL,
    display: str = "",
) -> None:
    """Lança Chrome no display real (para login manual)."""
    real_display = display or os.environ.get("DISPLAY", ":0")
    launch_chrome(profile_dir=profile_dir, display=real_display, cdp_url=cdp_url)


# ---------------------------------------------------------------------------
# Chrome kill + session check
# ---------------------------------------------------------------------------

def kill_chrome(profile_dir: Path = CHROME_PROFILE) -> None:
    """Terminate the running Chrome process and clean up singleton files.

    Tries the tracked _chrome_process first, falls back to the PID in
    SingletonLock, then removes the lock files so a new instance can start.
    """
    global _chrome_process
    _log = __import__("logging").getLogger("ai-hub.chrome")

    pid: int | None = None

    if _chrome_process is not None and _chrome_process.poll() is None:
        pid = _chrome_process.pid
        _chrome_process.terminate()
        try:
            _chrome_process.wait(timeout=6)
        except subprocess.TimeoutExpired:
            _chrome_process.kill()
            _chrome_process.wait(timeout=3)
        _chrome_process = None
    else:
        lock_pid = _singleton_pid(profile_dir)
        if lock_pid is not None and _pid_alive(lock_pid):
            pid = lock_pid
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
            deadline = time.time() + 8
            while time.time() < deadline and _pid_alive(pid):
                time.sleep(0.3)
            if _pid_alive(pid):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
            time.sleep(0.3)

    for name in _SINGLETON_FILES:
        with contextlib.suppress(FileNotFoundError):
            (profile_dir / name).unlink()

    if pid:
        _log.info("Killed Chrome (pid=%s).", pid)
    else:
        _log.info("kill_chrome: no running Chrome found.")


def _check_chatgpt_session_live(cdp_url: str, gpt_url: str) -> bool:
    """Make an authenticated request to ChatGPT's session API using browser cookies."""
    try:
        with ChromeManager(cdp_url=cdp_url) as mgr:
            page = mgr.get_or_open_page(gpt_url or "https://chatgpt.com")
            resp = page.context.request.get(
                "https://chatgpt.com/api/auth/session",
                timeout=10_000,
            )
            if not resp.ok:
                return False
            data = resp.json()
            return bool(data.get("user") or data.get("accessToken"))
    except Exception as exc:
        __import__("logging").getLogger("ai-hub.chrome").warning(
            "Session check failed: %s", exc
        )
        return False


def check_chatgpt_session(cdp_url: str = CDP_URL, gpt_url: str = "") -> bool:
    """Return True if ChatGPT is logged in. Result is cached for _SESSION_CACHE_TTL seconds."""
    now = time.time()
    if (
        _session_cache["ok"] is not None
        and (now - _session_cache["checked_at"]) < _SESSION_CACHE_TTL
    ):
        return bool(_session_cache["ok"])
    result = _check_chatgpt_session_live(cdp_url, gpt_url)
    _session_cache["ok"] = result
    _session_cache["checked_at"] = now
    __import__("logging").getLogger("ai-hub.chrome").info(
        "Session check: logged_in=%s", result
    )
    return result


def invalidate_session_cache() -> None:
    """Force the next check_chatgpt_session() call to hit the live page."""
    _session_cache["ok"] = None
    _session_cache["checked_at"] = 0.0


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
        try:
            self._browser = self._playwright.chromium.connect_over_cdp(self._cdp_url)
        except Exception:
            # Clean up the playwright Node driver so its pipes don't leak when
            # Chrome is down and connect_over_cdp raises ECONNREFUSED.
            with contextlib.suppress(Exception):
                self._playwright.__exit__(None, None, None)
            self._playwright = None
            raise
        return self

    def __exit__(self, *_):
        with contextlib.suppress(Exception):
            if self._playwright is not None:
                self._playwright.__exit__(None, None, None)

    @property
    def context(self):
        ctxs = self._browser.contexts
        if ctxs:
            ctx = ctxs[0]
        else:
            ctx = self._browser.new_context()
        # Patch navigator.webdriver so Cloudflare Turnstile doesn't block headless Chrome.
        with contextlib.suppress(Exception):
            ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        return ctx

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


# ---------------------------------------------------------------------------
# Async variant — usa async_playwright para não conflitar com o event loop asyncio.
# Usado pelos endpoints FastAPI (send, poll) que rodam diretamente no loop asyncio.
# ---------------------------------------------------------------------------

class AsyncChromeManager:
    """Context manager assíncrono que conecta ao Chrome via async Playwright CDP."""

    def __init__(self, cdp_url: str = CDP_URL):
        self._cdp_url = cdp_url
        self._playwright = None
        self._browser = None

    async def __aenter__(self):
        from playwright.async_api import async_playwright
        self._pw_ctx = async_playwright()
        self._playwright = await self._pw_ctx.__aenter__()
        self._browser = await self._playwright.chromium.connect_over_cdp(self._cdp_url)
        return self

    async def __aexit__(self, *_):
        with contextlib.suppress(Exception):
            await self._pw_ctx.__aexit__(None, None, None)

    @property
    def context(self):
        ctxs = self._browser.contexts
        return ctxs[0] if ctxs else None

    async def _ensure_context(self):
        ctx = self.context
        if ctx is None:
            ctx = await self._browser.new_context()
        with contextlib.suppress(Exception):
            await ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        return ctx

    async def find_page(self, url_contains: str):
        ctx = await self._ensure_context()
        for page in ctx.pages:
            try:
                if url_contains in (page.url or ""):
                    return page
            except Exception:
                continue
        return None

    async def get_or_open_page(self, url: str):
        ctx = await self._ensure_context()
        base = url.split("?")[0]
        for page in ctx.pages:
            try:
                if (page.url or "").split("?")[0] == base:
                    return page
            except Exception:
                continue
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        return page

    async def send_message(self, url: str, text: str) -> bool:
        page = await self.get_or_open_page(url)
        try:
            selectors = ("textarea", "div[contenteditable='true']", "[role='textbox']")
            for sel in selectors:
                loc = page.locator(sel).last
                try:
                    await loc.wait_for(state="visible", timeout=5_000)
                    await loc.click()
                    await page.keyboard.type(text)
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
                    await page.locator(ss).wait_for(state="visible", timeout=3_000)
                    await page.locator(ss).click()
                    return True
                except Exception:
                    pass
            await page.keyboard.press("Enter")
            return True
        except Exception as e:
            import logging
            logging.getLogger("ai-hub.chrome").warning("async send_message error: %s", e)
            return False
