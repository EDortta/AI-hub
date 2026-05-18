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


class AIHubClient:
    def __init__(self, daemon_url: str = DAEMON_URL, project_path: str = ""):
        self.daemon_url = daemon_url.rstrip("/")
        self.project_path = project_path or str(Path.cwd())

    def _get(self, path: str, **kwargs) -> dict:
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as c:
            r = c.get(f"{self.daemon_url}{path}", **kwargs)
            r.raise_for_status()
            return r.json()

    def _post(self, path: str, json: dict, timeout: int = DEFAULT_TIMEOUT) -> dict:
        with httpx.Client(timeout=timeout) as c:
            r = c.post(f"{self.daemon_url}{path}", json=json)
            r.raise_for_status()
            return r.json()

    def _delete(self, path: str) -> dict:
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as c:
            r = c.delete(f"{self.daemon_url}{path}")
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
        }, timeout=120)

    def send_to_conversation(self, watcher_id: str, text: str) -> dict:
        return self._post(f"/conversations/{watcher_id}/send", {"text": text})

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
