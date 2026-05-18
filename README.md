# AI-Hub

Shared Chrome daemon for multi-project ChatGPT access.

## Problem

Multiple local projects (Dopamin Captain, IgrejaPequena, GestaoContasFernanda) each
launched their own Chrome on port 9222, causing conflicts and Chrome windows popping up unexpectedly.

## Solution

`chrome-daemon` is the single owner of Chrome. It runs hidden via Xvfb and exposes an
HTTP API (port 9400). Projects register via `.ai-hub.yml` and receive callbacks when
new messages arrive in their conversation.

## Setup

```bash
cd chrome-daemon
bash install/install.sh
ai-hub setup    # log into ChatGPT (Chrome will appear briefly)
```

## Project registration

Create `.ai-hub.yml` in your project root:

```yaml
conversations:
  - url: "https://chatgpt.com/c/YOUR-CONVERSATION-ID"
    alias: "Claudia"            # messages starting with "Claudia," route here
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
| GET | /setup | Show Chrome for login |
| POST | /conversations/register | Register a conversation watcher |
| DELETE | /conversations/{id} | Remove watcher |
| GET | /conversations | List all watchers with state |
| POST | /image/generate | Generate image via ChatGPT GPT |

## Message routing

When you type `"Claudia, faça X"` in ChatGPT, the daemon:
1. Detects the alias prefix (`Claudia`)
2. Finds the registered watcher with that alias
3. POSTs to its `callback_url` with `{watcher_id, alias, text, role, turn_id}`

## Timing states

Each watcher alternates between:
- **INTERACTION** — recent activity → polls every `interaction_poll_seconds`  
- **LATENCY** — silence → polls every `latency_poll_seconds`
