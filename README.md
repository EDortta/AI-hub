# AI-Hub

Shared Chrome daemon for multi-project ChatGPT access.

## Problem

Multiple local projects (Dopamin Captain, IgrejaPequena, GestaoContasFernanda) each
launched their own Chrome on port 9222, causing conflicts and Chrome windows popping up
unexpectedly.

## Solution

`chrome-daemon` is the single owner of Chrome. It runs hidden (Xvfb when available,
`--headless=new` otherwise) and exposes an HTTP API on port 9400. Projects register
via `.ai-hub.yml` and receive callbacks when new messages arrive in their conversation.

## Setup

```bash
cd chrome-daemon
bash install/install.sh
ai-hub setup    # opens Chrome visibly so you can log into ChatGPT
```

`install.sh` also runs `loginctl enable-linger` so the daemon survives logout.

## Project registration

Create `.ai-hub.yml` in your project root:

```yaml
conversations:
  - url: "https://chatgpt.com/c/YOUR-CONVERSATION-ID"
    alias: "Claudia"            # local routing key — messages starting "Claudia," route here
    chatgpt_alias: "Sofia"      # name of the ChatGPT persona (informational)
    purpose: "What this agent does"
    interaction_poll_seconds: 5
    latency_poll_seconds: 60
    callback_url: "http://localhost:9401/message"

image_generators:
  - gpt_url: "https://chatgpt.com/g/g-xxxxx-your-gpt"
    alias: "MyGPT"
```

Then register:

```bash
cd /path/to/your/project
ai-hub register
```

## API

| Method | Path | Description |
|--------|------|-------------|
| GET | /status | Daemon health + watcher count |
| GET | /setup | Show Chrome for manual login |
| POST | /conversations/register | Register a conversation watcher |
| DELETE | /conversations/{id} | Remove watcher by ID |
| DELETE | /conversations/by-project/{path} | Remove all watchers for a project |
| GET | /conversations | List all watchers with current state |
| POST | /conversations/{id}/send | Post a message to the conversation |
| GET | /conversations/{id}/last-message | Fetch the last assistant message |
| POST | /image/generate | Generate image via a ChatGPT GPT |
| POST | /social/publish/x | Publish image + caption to X (Twitter) |
| POST | /social/publish/linkedin | Publish image + caption to LinkedIn |

### Image generation

```json
POST /image/generate
{
  "gpt_url": "https://chatgpt.com/g/g-xxxxx-your-gpt",
  "prompt": "minimalist blue logo",
  "orientation": "portrait",
  "output_dir": "/path/to/save",
  "greeting": "Hey, ",
  "reference_image_path": "/path/to/reference.png"
}
```

`reference_image_path` is optional. When provided, the image is attached to the prompt
before generation (style reference / img2img).

### Social publishing

```json
POST /social/publish/x
{
  "image_path": "/path/to/image.png",
  "caption": "Post text",
  "url": "https://x.com/compose/post"
}
```

LinkedIn uses the same body shape at `/social/publish/linkedin`.

## Message routing

When `"Claudia, faça X"` appears in a watched ChatGPT conversation, the daemon:
1. Detects the alias prefix (`Claudia`)
2. Finds the registered watcher with that alias
3. POSTs to its `callback_url`:

```json
{"watcher_id": "...", "alias": "Claudia", "text": "faça X", "role": "user", "turn_id": "..."}
```

Multiple projects can watch the same conversation using different aliases.

## Timing states

Each watcher switches between two polling modes:

- **INTERACTION** — recent activity → polls every `interaction_poll_seconds` (e.g. 5 s)
- **LATENCY** — silence → polls every `latency_poll_seconds` (e.g. 60 s)

Transition to LATENCY starts counting from the last message *received*, not sent.

## Playwright thread safety

All Playwright calls run in a dedicated `ThreadPoolExecutor` (`playwright_executor` in
`chrome_manager.py`) whose threads have no asyncio event loop. This prevents the
*"Playwright Sync API inside asyncio loop"* error that occurs when Playwright is
invoked from `asyncio.get_event_loop().run_in_executor(None, ...)` — the default
executor inherits the running loop from the asyncio thread.

Rule: every `run_in_executor` call that touches Playwright must use
`playwright_executor`, never `None`.

## Chrome profile

The shared Chrome profile lives at `~/.local/share/ai-hub/chrome-profile/`.
It holds the authenticated ChatGPT session. Do not delete it.

If the session expires, run `ai-hub setup` to log in again.

## Xvfb vs headless

`ensure_xvfb()` returns `""` when Xvfb is not installed — `launch_chrome()` then uses
`--headless=new` instead of a virtual display. Either mode is transparent to callers.
