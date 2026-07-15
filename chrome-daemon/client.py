"""AI-Hub client library.

Helper for project scripts to communicate with the ai-hub daemon.
Reads .ai-hub.yml from the project root and registers conversations/image generators.

Usage:
    from client import AIHubClient
    client = AIHubClient()
    client.register_from_config()          # reads .ai-hub.yml
    client.generate_image(gpt_url, prompt) # returns Path
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx
import yaml

DAEMON_URL = os.environ.get("AI_HUB_URL", "http://127.0.0.1:9400")
DEFAULT_TIMEOUT = 30

# SEC-0001: shared token sent as a Bearer header on every request. Read from the
# environment; never hardcode. The daemon rejects requests without it (401).
DAEMON_TOKEN = os.environ.get("AIHUB_DAEMON_TOKEN", "").strip()


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {DAEMON_TOKEN}"} if DAEMON_TOKEN else {}


class SessionExpiredError(RuntimeError):
    """Raised when the daemon reports that the ChatGPT session has expired."""


class AIHubClient:
    def __init__(self, daemon_url: str = DAEMON_URL, project_path: str = ""):
        self.daemon_url = daemon_url.rstrip("/")
        self.project_path = project_path or str(Path.cwd())

    def _get(self, path: str, **kwargs) -> dict:
        headers = {**_auth_headers(), **kwargs.pop("headers", {})}
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as c:
            r = c.get(f"{self.daemon_url}{path}", headers=headers, **kwargs)
            r.raise_for_status()
            return r.json()

    def _post(self, path: str, json: dict, timeout: int = DEFAULT_TIMEOUT) -> dict:
        with httpx.Client(timeout=timeout) as c:
            r = c.post(f"{self.daemon_url}{path}", json=json, headers=_auth_headers())
            if not r.is_success:
                detail = ""
                try:
                    detail = r.json().get("detail", "")
                except Exception:
                    pass
                if r.status_code == 401 and detail == "chatgpt_session_expired":
                    raise SessionExpiredError(
                        "ChatGPT session expired. Use hub.open_login() to re-authenticate."
                    )
                msg = f"HTTP {r.status_code} from {path}"
                if detail:
                    msg += f": {detail}"
                raise RuntimeError(msg)
            return r.json()

    def _delete(self, path: str) -> dict:
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as c:
            r = c.delete(f"{self.daemon_url}{path}", headers=_auth_headers())
            r.raise_for_status()
            return r.json()

    def status(self) -> dict:
        return self._get("/status")

    def is_alive(self) -> bool:
        try:
            self.status()
            return True
        except Exception:
            return False

    def check_session(self, gpt_url: str = "") -> bool:
        """Return True if ChatGPT is logged in (forces a live check)."""
        try:
            result = self._get(f"/session/check?gpt_url={gpt_url}")
            return bool(result.get("logged_in"))
        except Exception:
            return False

    def open_login(self, display: str = ":0") -> None:
        """Stop headless Chrome and open a visible window for manual login."""
        self._post("/session/login", {"display": display}, timeout=60)

    def confirm_login(self) -> None:
        """Signal that login is done; restarts headless Chrome."""
        self._post("/session/login-done", {}, timeout=60)

    def register_conversation(
        self,
        url: str,
        alias: str = "",
        chatgpt_alias: str = "",
        purpose: str = "",
        interaction_poll_seconds: int = 5,
        latency_poll_seconds: int = 60,
        callback_url: str = "",
    ) -> dict:
        return self._post("/conversations/register", {
            "url": url,
            "alias": alias,
            "chatgpt_alias": chatgpt_alias,
            "purpose": purpose,
            "interaction_poll_seconds": interaction_poll_seconds,
            "latency_poll_seconds": latency_poll_seconds,
            "callback_url": callback_url,
            "project_path": self.project_path,
        })

    def unregister_project(self) -> dict:
        import urllib.parse
        enc = urllib.parse.quote(self.project_path, safe="")
        return self._delete(f"/conversations/by-project/{enc}")

    def list_conversations(self) -> list[dict]:
        return self._get("/conversations")

    def generate_image(
        self,
        gpt_url: str,
        prompt: str,
        orientation: str = "portrait",
        output_dir: str = "",
        greeting: str = "Hey, ",
        reference_image_path: "Path | None" = None,
    ) -> Path:
        result = self._post("/image/generate", {
            "gpt_url": gpt_url,
            "prompt": prompt,
            "orientation": orientation,
            "output_dir": output_dir,
            "greeting": greeting,
            "reference_image_path": str(reference_image_path) if reference_image_path else "",
        }, timeout=700)
        return Path(result["image_path"])

    def generate_image_bytes(
        self,
        gpt_url: str,
        prompt: str,
        orientation: str = "portrait",
        greeting: str = "Hey, ",
        reference_image_path: "Path | None" = None,
    ) -> "tuple[bytes, str]":
        """Generate an image and return its raw bytes + filename, in one call.

        Works across hosts: the daemon inlines the image (base64) so the caller
        never needs filesystem access to the daemon host. Use this instead of
        generate_image() when the daemon runs on another machine.
        """
        import base64
        result = self._post("/image/generate", {
            "gpt_url": gpt_url,
            "prompt": prompt,
            "orientation": orientation,
            "output_dir": "",
            "greeting": greeting,
            "reference_image_path": str(reference_image_path) if reference_image_path else "",
            "include_bytes": True,
        }, timeout=700)
        return base64.b64decode(result["image_b64"]), result.get("filename", "image.png")

    def fetch_image(self, image_path: "str | Path", dest: "str | Path | None" = None) -> bytes:
        """Fetch the bytes of an already-generated image by its daemon-side path.

        Returns the bytes; if `dest` is given, also writes them there. Lets a
        remote caller retrieve an image the daemon produced (the /image/generate
        response only carries the path unless include_bytes was set).
        """
        headers = _auth_headers()
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as c:
            r = c.get(f"{self.daemon_url}/image/fetch",
                      params={"path": str(image_path)}, headers=headers)
            r.raise_for_status()
            data = r.content
        if dest is not None:
            Path(dest).write_bytes(data)
        return data

    def publish_to_x(
        self,
        image_path: "Path",
        caption: str,
        x_compose_url: str = "https://x.com/compose/post",
    ) -> None:
        self._post("/social/publish/x", {
            "image_path": str(image_path),
            "caption": caption,
            "url": x_compose_url,
        }, timeout=120)

    def publish_to_linkedin(
        self,
        image_path: "Path",
        caption: str,
        linkedin_url: str = "https://www.linkedin.com/feed/",
    ) -> None:
        self._post("/social/publish/linkedin", {
            "image_path": str(image_path),
            "caption": caption,
            "url": linkedin_url,
        }, timeout=300)

    def send_to_conversation(self, watcher_id: str, text: str) -> dict:
        return self._post(f"/conversations/{watcher_id}/send", {"text": text}, timeout=120)

    def get_inbox(self, watcher_id: str) -> list[dict]:
        """Return pending inbox messages addressed to this watcher's alias."""
        result = self._get(f"/conversations/{watcher_id}/inbox")
        return result.get("inbox", [])

    def clear_inbox(self, watcher_id: str) -> dict:
        """Clear all inbox messages for this watcher."""
        return self._delete(f"/conversations/{watcher_id}/inbox")

    def get_last_message(self, watcher_id: str) -> dict | None:
        result = self._get(f"/conversations/{watcher_id}/last-message")
        return result.get("message")

    def register_from_config(self, config_path: Path | None = None) -> list[dict]:
        """Reads .ai-hub.yml and registers all conversations."""
        if config_path is None:
            config_path = Path(self.project_path) / ".ai-hub.yml"
        if not config_path.exists():
            raise FileNotFoundError(f".ai-hub.yml not found at {config_path}")

        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        registered = []

        for conv in cfg.get("conversations", []):
            result = self.register_conversation(
                url=conv["url"],
                alias=conv.get("alias", ""),
                chatgpt_alias=conv.get("chatgpt_alias", ""),
                purpose=conv.get("purpose", ""),
                interaction_poll_seconds=conv.get("interaction_poll_seconds", 5),
                latency_poll_seconds=conv.get("latency_poll_seconds", 60),
                callback_url=conv.get("callback_url", ""),
            )
            registered.append(result)

        return registered
