# DIAG-20260707 — Chrome daemon ativo, mas rejeitando endpoints por token ausente

- work_id: WK-20260707-aihub-daemon-token
- date: 2026-07-07
- origem: diagnóstico wa-hub sobre subsidiários sem resposta

## Contexto
O `chrome-daemon.service` está `active (running)` em `127.0.0.1:9400`, mas seus endpoints retornam 503.

Log observado:

`AIHUB_DAEMON_TOKEN is not set — all endpoints will reject requests with 503. Export AIHUB_DAEMON_TOKEN to enable the daemon.`

Isso afeta clientes que dependem do AI-Hub/ChatGPT browser bridge, incluindo fluxos do GCF e possíveis fluxos de imagem/persona.

## Objetivo
Garantir que o `chrome-daemon.service` suba com `AIHUB_DAEMON_TOKEN` configurado de forma segura e persistente.

## Escopo
- Localizar fonte esperada do token sem imprimir o segredo.
- Ajustar a unidade systemd ou EnvironmentFile para carregar o token.
- Validar `/status` ou endpoint equivalente com autenticação correta.

## Fora de escopo
- Trocar provider de LLM.
- Expor token em issue, log ou shell history.

## ARO / Plano de teste
- `systemctl --user status chrome-daemon.service --no-pager`
- chamada autenticada ao status do daemon
- confirmar ausência de 503 por token ausente

## DoD
- Daemon responde com status operacional autenticado.
- Token não aparece em logs, issue ou commit.
- Dependentes podem voltar a consumir o AI-Hub.

## Resolução (2026-07-07)
Rollout de token compartilhado, fail-closed, sem segredo em repo:

1. **Segredo** — `~/.config/ai-hub/daemon.env` (chmod 600, fora de repo):
   `AIHUB_DAEMON_TOKEN=<openssl rand -hex 32>`.
2. **Daemon** — drop-in `~/.config/systemd/user/chrome-daemon.service.d/token.conf`
   com `EnvironmentFile=%h/.config/ai-hub/daemon.env`. Journal: `Daemon auth enabled`.
3. **AI-Gateway** — `aihub_driver._client()` passou a enviar `Authorization: Bearer`
   (novo setting `aihub_daemon_token` via `validation_alias="AIHUB_DAEMON_TOKEN"`,
   config.py); drop-in `ai-gateway.service.d/aihub-token.conf` injeta o mesmo token.
4. **Guardião** — `~/scripts/ai-hub-guardian.sh` passou a mandar o Bearer no
   health check (antes o `curl -sf /status` sem token dava 401/503 e o guardião
   **matava o daemon são** — era a causa do SIGTERM recorrente).

**Validado:** `/status` sem token → 401; com token → 200 (`chrome_cdp_available:true`);
`AiHubDriver.health()` → `('UP', None)`; guardião exit 0 (failures=0).

**Residual:** `chatgpt_logged_in: null` (sessão ChatGPT precisa de login manual pelo
operador — `/session/login`); gcf-bridge (GestaoContasFernanda) ainda **não** recebeu
o token (fora deste escopo, serviço está dead). Mudanças de código/config **não
commitadas** — aguardam pedido.

