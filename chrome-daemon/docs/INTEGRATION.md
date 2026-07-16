# AI-Hub — Guia de Integração

> Documento único para integrar um aplicativo/agente ao AI-Hub.
> Servido ao vivo pelo próprio daemon em `GET /llms.txt` e `GET /docs/integration.md`.

## O que é

O AI-Hub é um **daemon HTTP** que é dono de uma **instância única do Chrome**,
logada com as credenciais do operador (ChatGPT, Google, X, LinkedIn, etc.).
Vários aplicativos compartilham essa mesma sessão através de uma API autenticada,
sem cada um ter de fazer login nem gerenciar um browser.

Duas formas de uso:

1. **API HTTP** (recomendada) — chat com ChatGPT, geração de imagem, publicação
   social, navegação com screenshot. É o que este documento descreve.
2. **CDP direto** — pilotar o Chrome logado via Playwright/Chrome DevTools Protocol,
   para automação livre. Requer túnel (ver a seção *Chrome remoto (CDP)*).

## Base URL e autenticação

- **Base URL:** `http://<host>:9480` (via nginx). Ex.: `http://192.168.7.200:9480`.
- **Auth:** toda rota (exceto as de documentação) exige um token compartilhado no
  header `Authorization: Bearer <TOKEN>`.
- **Fail-closed:** sem token → `401`; se o daemon não tiver token configurado →
  `503`. Nunca há acesso anônimo às rotas de operação.
- O token é provisionado fora de qualquer repositório. Peça-o ao operador e
  **nunca** o registre em código, log ou histórico.

Host allowlist: o daemon rejeita `Host` inesperado com `403` (guarda anti
DNS-rebinding). Pelo nginx padrão isso já está resolvido.

## Início rápido

```bash
BASE=http://192.168.7.200:9480
TOKEN=<seu-token>

# 1. saúde do daemon
curl -s -H "Authorization: Bearer $TOKEN" $BASE/status

# 2. a sessão do ChatGPT está logada?
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/session/check"
```

```python
# Python — cliente oficial (chrome-daemon/client.py)
import os
os.environ["AI_HUB_URL"] = "http://192.168.7.200:9480"
os.environ["AIHUB_DAEMON_TOKEN"] = "<seu-token>"

from client import AIHubClient          # copie client.py para o seu projeto
hub = AIHubClient()
print(hub.status())
print(hub.check_session())              # True/False
```

## Endpoints

Todos exigem `Authorization: Bearer <TOKEN>`. Corpos são JSON.

### Saúde e sessão
| Método | Caminho | Descrição |
|---|---|---|
| GET | `/status` | Saúde: `chrome_cdp_available`, `watchers`, `chatgpt_logged_in`, `display`. |
| GET | `/session/check?gpt_url=` | Faz um check ao vivo do login do ChatGPT. `{"logged_in": bool}`. |
| POST | `/session/login` | Abre Chrome visível para login manual (uso do operador; não requer no fluxo normal). |
| POST | `/session/login-done` | Fecha o Chrome visível e retoma o headless. |

### Conversas com ChatGPT (modelo de *watcher*)
O AI-Hub acompanha conversas do ChatGPT por **alias**. Você registra uma conversa
(URL do chat) com um alias; o daemon monitora mensagens novas e as entrega no seu
`callback_url` (HTTP POST). Você envia mensagens por `send`.

| Método | Caminho | Descrição |
|---|---|---|
| POST | `/conversations/register` | Registra uma conversa. Corpo abaixo. Retorna o watcher (com `id`). |
| GET | `/conversations` | Lista os watchers ativos. |
| DELETE | `/conversations/{watcher_id}` | Remove um watcher. |
| DELETE | `/conversations/by-project/{project_path}` | Remove watchers de um projeto. |
| POST | `/conversations/{watcher_id}/send` | Envia texto para a conversa. Corpo: `{"text": "..."}`. |
| GET | `/conversations/{watcher_id}/last-message` | Última resposta do assistente. |
| GET | `/conversations/{watcher_id}/inbox` | Mensagens pendentes endereçadas ao alias. |
| DELETE | `/conversations/{watcher_id}/inbox` | Limpa a inbox. |

Corpo de `register`:
```json
{
  "url": "https://chatgpt.com/c/<id-da-conversa>",
  "alias": "MeuApp",
  "chatgpt_alias": "Sofia",
  "purpose": "descrição curta do papel deste app",
  "interaction_poll_seconds": 5,
  "latency_poll_seconds": 60,
  "callback_url": "http://<seu-host>:<porta>/message",
  "project_path": "/caminho/opcional/do/projeto"
}
```

Callback: quando chega mensagem nova, o daemon faz `POST callback_url` com um JSON
da mensagem. **O callback não leva autenticação** — proteja a porta por firewall
(aceite só o IP do host do daemon).

> **Importante:** o registro de watchers é **em memória**. Um restart do daemon
> apaga todos os watchers — seu app deve **re-registrar por alias** ao (re)subir.

### Geração de imagem (ChatGPT)
| Método | Caminho | Descrição |
|---|---|---|
| POST | `/image/generate` | Gera imagem via um GPT do ChatGPT. |
| GET | `/image/fetch?path=` | Faz stream dos bytes de uma imagem já gerada. |

Corpo de `/image/generate`:
```json
{
  "gpt_url": "https://chatgpt.com/g/<id-do-gpt>",
  "prompt": "descrição da imagem",
  "orientation": "portrait",
  "greeting": "Hey, ",
  "reference_image_path": "",
  "include_bytes": true,
  "delete_chat": false
}
```

Resposta (com `include_bytes: true`):
```json
{
  "ok": true,
  "image_path": "/home/ai-hub/.local/share/ai-hub/images/....png",
  "filename": "....png",
  "content_type": "image/png",
  "image_b64": "<base64 da imagem>"
}
```

- `image_path` é um caminho **no host do daemon** — não tente lê-lo diretamente se
  seu app roda em outra máquina.
- Use `include_bytes: true` para receber a imagem na mesma resposta (base64), **ou**
  chame `GET /image/fetch?path=<image_path>` para baixar os bytes depois.
- `output_dir` existe mas é confinado a diretórios permitidos do host do daemon;
  clientes remotos devem deixá-lo vazio (o daemon usa seu diretório padrão seguro).

### Publicação social
| Método | Caminho | Corpo |
|---|---|---|
| POST | `/social/publish/x` | `{"image_path": "...", "caption": "...", "url": "..."}` |
| POST | `/social/publish/linkedin` | idem |

`image_path` precisa existir **no host do daemon** e dentro dos diretórios
permitidos (gere a imagem no próprio daemon antes).

### Navegação genérica (screenshot / ações)
| Método | Caminho | Descrição |
|---|---|---|
| POST | `/browse?url=&wait_ms=3000` | Abre a URL na sessão logada; retorna título, links/botões e `screenshot_b64`. |
| POST | `/page/action?url=&action=&selector=&value=` | `click` / `type` / `evaluate` / `screenshot` numa página aberta. |
| GET | `/debug/screenshot?url_contains=chatgpt` | Screenshot de diagnóstico da página que casa. |

## Modelo de erros

| Código | Significado |
|---|---|
| `401` | Token ausente/errado, **ou** `chatgpt_session_expired` (sessão do ChatGPT caiu). |
| `403` | `host_not_allowed` (Host header fora da allowlist). |
| `400` | Path fora dos diretórios permitidos (`*_outside_allowed_paths`) ou arquivo ausente. |
| `404` | Watcher/imagem não encontrados. |
| `503` | Daemon sem token configurado, ainda subindo, ou login manual em andamento. |

O corpo de erro é `{"detail": "<código>"}`. Trate `chatgpt_session_expired`
pedindo ao operador para refazer o login (a sessão web expira por IP/fingerprint).

## Cliente Python (`client.py`)

`AIHubClient` (em `chrome-daemon/client.py`) encapsula tudo. Config por env:
`AI_HUB_URL` (default `http://127.0.0.1:9400`) e `AIHUB_DAEMON_TOKEN`.

```python
from client import AIHubClient
hub = AIHubClient()

# imagem, cross-host (bytes na hora):
data, filename = hub.generate_image_bytes(
    gpt_url="https://chatgpt.com/g/<gpt>",
    prompt="um gato astronauta",
)
open(filename, "wb").write(data)

# ou buscar depois, por caminho:
img = hub.fetch_image("/home/ai-hub/.local/share/ai-hub/images/x.png", dest="x.png")
```

## Chrome remoto (CDP) — pilotar o browser logado

Para automação livre dentro da sessão logada (pesquisar, raspar, preencher),
use `chrome-daemon/remote_chrome.py`. Ele abre um **túnel SSH** até o CDP do host
do daemon (o CDP **não** é exposto na rede — não tem autenticação) e conecta o
Playwright.

```bash
# CLI
python3 chrome-daemon/remote_chrome.py https://algum-site-logado.com
python3 chrome-daemon/remote_chrome.py https://x.com/home --screenshot /tmp/x.png
```

```python
from remote_chrome import RemoteChrome
with RemoteChrome() as rc:          # abre/fecha o túnel sozinho
    rc.open("https://chatgpt.com")
    print(rc.text())                # innerText do body
    rc.screenshot("/tmp/shot.png")
```

Config: `AIHUB_CDP_SSH` (host SSH do daemon), `AIHUB_CDP_PORT` (default 9222).

## Limites e boas práticas

- **Sessão única compartilhada:** todos os apps usam o mesmo Chrome logado. Evite
  ações destrutivas (logout, trocar conta).
- **Watchers são voláteis:** re-registre por alias ao subir.
- **Sessões web expiram:** trate `401 chatgpt_session_expired`; re-login é manual.
- **Uma operação pesada por vez:** geração de imagem/publicação serializam (semáforo);
  chamadas concorrentes podem esperar.
- **Caminhos de arquivo são do host do daemon:** para trocar bytes entre hosts use
  `include_bytes`/`/image/fetch`, nunca leia `image_path` diretamente de outra máquina.
- **CDP nunca exposto:** acesso ao browser cru é só por túnel SSH.
