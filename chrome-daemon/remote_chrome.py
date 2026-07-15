#!/usr/bin/env python3
"""Acesso ao Chrome remoto do AI-hub (stage4), logado com as credenciais do operador.

Sintetiza tudo o que é preciso para "pesquisar usando o chrome do ai-hub":
  1. abre um túnel SSH sob demanda até o CDP (127.0.0.1:9222) do stage4
     — o CDP não é exposto na rede porque não tem autenticação;
  2. conecta o Playwright ao Chrome logado por esse túnel;
  3. oferece navegação simples (abrir URL, extrair texto/HTML, screenshot);
  4. fecha o túnel ao sair.

Se um CDP já estiver acessível em 127.0.0.1:9222 (túnel/daemon local já de pé),
reaproveita e não abre outro.

USO — linha de comando
    # texto de uma página (dentro da sessão logada)
    python3 remote_chrome.py https://chatgpt.com

    # salva screenshot
    python3 remote_chrome.py https://x.com/home --screenshot /tmp/x.png

    # HTML cru
    python3 remote_chrome.py https://news.ycombinator.com --html

USO — como biblioteca
    from remote_chrome import RemoteChrome
    with RemoteChrome() as rc:
        page = rc.open("https://chatgpt.com")
        print(rc.text())
        # page é um objeto Playwright normal para automação avançada
        rc.screenshot("/tmp/shot.png")

Configuração (variáveis de ambiente, todas com default sensato):
    AIHUB_CDP_SSH    host SSH do stage4         (default: stage4-inovacao)
    AIHUB_CDP_PORT   porta CDP local/remota     (default: 9222)
"""
from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import sys
import time
import urllib.request

CDP_PORT = int(os.environ.get("AIHUB_CDP_PORT", "9222"))
SSH_HOST = os.environ.get("AIHUB_CDP_SSH", "stage4-inovacao")
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"


def _cdp_up(timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=timeout):
            return True
    except Exception:
        return False


class RemoteChrome:
    """Context manager: garante o túnel CDP e conecta o Playwright ao Chrome remoto."""

    def __init__(self, ssh_host: str = SSH_HOST, cdp_port: int = CDP_PORT):
        self._ssh_host = ssh_host
        self._cdp_port = cdp_port
        self._cdp_url = f"http://127.0.0.1:{cdp_port}"
        self._tunnel: subprocess.Popen | None = None
        self._owns_tunnel = False
        self._pw = None
        self._browser = None
        self._page = None

    # -- túnel ------------------------------------------------------------
    def _ensure_tunnel(self) -> None:
        if _cdp_up():
            # Já há CDP local (túnel manual ou daemon local) — reaproveita.
            return
        cmd = [
            "ssh", "-N",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=15",
            "-L", f"{self._cdp_port}:127.0.0.1:{self._cdp_port}",
            self._ssh_host,
        ]
        self._tunnel = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._owns_tunnel = True
        deadline = time.time() + 20
        while time.time() < deadline:
            if _cdp_up():
                return
            if self._tunnel.poll() is not None:
                raise RuntimeError(
                    f"Túnel SSH para {self._ssh_host} caiu antes de o CDP responder. "
                    "Verifique 'ssh {self._ssh_host}' e se o daemon está de pé no stage4."
                )
            time.sleep(0.5)
        raise RuntimeError(
            f"CDP não respondeu em {self._cdp_url} em 20s pelo túnel para {self._ssh_host}."
        )

    def _close_tunnel(self) -> None:
        if self._tunnel is not None and self._owns_tunnel:
            with contextlib.suppress(Exception):
                self._tunnel.terminate()
                self._tunnel.wait(timeout=5)
            self._tunnel = None

    # -- ciclo de vida ----------------------------------------------------
    def __enter__(self) -> "RemoteChrome":
        self._ensure_tunnel()
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().__enter__()
        self._browser = self._pw.chromium.connect_over_cdp(self._cdp_url)
        return self

    def __exit__(self, *_exc) -> None:
        with contextlib.suppress(Exception):
            if self._pw is not None:
                self._pw.__exit__(None, None, None)
        self._close_tunnel()

    # -- contexto/página --------------------------------------------------
    @property
    def context(self):
        ctxs = self._browser.contexts
        return ctxs[0] if ctxs else self._browser.new_context()

    def open(self, url: str, wait: str = "domcontentloaded", timeout_ms: int = 60_000):
        """Abre uma nova aba na sessão logada e navega até `url`. Devolve a page Playwright."""
        page = self.context.new_page()
        page.goto(url, wait_until=wait, timeout=timeout_ms)
        with contextlib.suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=10_000)
        self._page = page
        return page

    def text(self, page=None) -> str:
        """Texto visível da página (innerText do body)."""
        p = page or self._page
        if p is None:
            raise RuntimeError("Nenhuma página aberta — chame open(url) primeiro.")
        return p.eval_on_selector("body", "el => el.innerText")

    def html(self, page=None) -> str:
        p = page or self._page
        if p is None:
            raise RuntimeError("Nenhuma página aberta — chame open(url) primeiro.")
        return p.content()

    def screenshot(self, path: str, page=None, full_page: bool = True) -> str:
        p = page or self._page
        if p is None:
            raise RuntimeError("Nenhuma página aberta — chame open(url) primeiro.")
        p.screenshot(path=path, full_page=full_page)
        return path


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Acessa o Chrome logado do AI-hub (stage4) e extrai conteúdo de uma URL.",
    )
    ap.add_argument("url", help="URL a abrir dentro da sessão logada")
    ap.add_argument("--html", action="store_true", help="imprime o HTML cru em vez do texto")
    ap.add_argument("--screenshot", metavar="ARQUIVO", help="salva screenshot full-page no caminho dado")
    ap.add_argument("--ssh", default=SSH_HOST, help=f"host SSH do stage4 (default: {SSH_HOST})")
    args = ap.parse_args()

    try:
        with RemoteChrome(ssh_host=args.ssh) as rc:
            rc.open(args.url)
            if args.screenshot:
                rc.screenshot(args.screenshot)
                print(f"[screenshot] {args.screenshot}", file=sys.stderr)
            sys.stdout.write(rc.html() if args.html else rc.text())
            sys.stdout.write("\n")
        return 0
    except Exception as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
