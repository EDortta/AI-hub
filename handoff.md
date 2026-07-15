# Handoff — chrome-daemon

## Topologia atual (desde 2026-07-15, WK-20260715-aihub-stage4)

O chrome-daemon **roda no stage4** (`root@192.168.7.200`, VM Proxmox Debian 12),
não mais no devel3.

- **Host:** stage4. Usuário dedicado **`ai-hub`** (systemd user service + linger).
  Nunca root (SEC-0107: Chrome sem `--no-sandbox`).
- **Chrome:** Xvfb `:99`, CDP em `127.0.0.1:9222` (loopback — **nunca exposto**).
- **API:** uvicorn em `127.0.0.1:9400`, exposta na LAN via **nginx vhost próprio**
  `/etc/nginx/sites-available/ai-hub.conf` → `listen 9480`. Independente da ZeeCred
  (`steward.conf` intocado).
- **Auth:** `AIHUB_DAEMON_TOKEN` (mesmo valor do devel3) em
  `/home/ai-hub/.config/ai-hub/daemon.env` + `AIHUB_ALLOWED_HOSTS=192.168.7.200,stage4`.
- **Guardian:** `/usr/local/sbin/ai-hub-guardian.sh` via `/etc/cron.d/ai-hub`
  (root, restart via `systemctl --user -M ai-hub@`). process_monitor no mesmo cron
  (usuário ai-hub).

### Acesso dos clientes
- **API (chat/browser/imagem):** `AI_HUB_URL=http://192.168.7.200:9480` + token.
  O **AI Gateway** (devel3, `ai-gateway.service`) já aponta para lá via
  `AIGW_AIHUB_BASE_URL` no `.env`.
- **CDP direto ("pilotar o chrome logado"):** túnel SSH sob demanda —
  `~/scripts/aihub-cdp-tunnel.sh` no devel3 (`ssh -L 9222 stage4-inovacao`),
  depois conectar Playwright em `127.0.0.1:9222`.
- **Re-login manual:** `x11vnc -display :99 -localhost` no stage4 +
  `ssh -L 5900:127.0.0.1:5900 stage4-inovacao`.

### devel3 (rollback preservado)
- `chrome-daemon.service`: **inactive + disabled** (não apagado; perfil local intacto).
- Crons `ai-hub-guardian` e `process_monitor` neutralizados
  (`#AIHUB-PAUSED#` / `#AIHUB-MOVED-STAGE4#`).
- Rollback: reverter `AIGW_AIHUB_BASE_URL`, `systemctl --user enable --now chrome-daemon`.

## Acesso ao Chrome remoto (script único)
`chrome-daemon/remote_chrome.py` no devel3 sintetiza tudo: abre o túnel SSH ao CDP
do stage4, conecta o Playwright no Chrome logado e extrai conteúdo. Túnel fecha ao
sair; reaproveita CDP local se já houver um.
- CLI: `python3 chrome-daemon/remote_chrome.py https://url [--html|--screenshot f.png]`
- Lib: `from remote_chrome import RemoteChrome; with RemoteChrome() as rc: rc.open(url); rc.text()`
- Env: `AIHUB_CDP_SSH` (default `stage4-inovacao`), `AIHUB_CDP_PORT` (9222).

## Geração de imagem cross-host (resolvido)
A própria API devolve a imagem, sem depender do filesystem do stage4:
- `POST /image/generate` com `include_bytes:true` → resposta traz `image_b64`+`filename`.
- `GET /image/fetch?path=<image_path>` → stream dos bytes (confinado SEC-0108).
- `client.py`: `generate_image_bytes()` e `fetch_image()`.
- **Gateway** já usa isso: pede bytes, salva no `aihub_image_output_dir` local e
  expõe `b64_json` + `path` local em `/v1/images/generations`.

## Pendências conhecidas
- **Sessões web** invalidam por IP/fingerprint — re-login é passo manual da migração,
  não bug (ver docs/napkin-lessons.md).
- **X/Twitter:** cookie presente e `/home` carrega, mas a conta pode ter desafio de
  verificação disparado no login manual (lado do X).
- **Registry de watchers volátil:** restart zera; consumidores re-registram por alias.
