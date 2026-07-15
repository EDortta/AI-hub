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

## Pendências conhecidas
- **Geração de imagem cross-host:** `/image/generate` devolve `image_path` no
  filesystem do stage4; o `output_dir` relativo do Gateway (`data/aihub-images`)
  cai fora do path-confinement do stage4. Precisa de endpoint `GET /image/fetch`
  autenticado (ou retorno base64) antes de usar imagem via Gateway remoto. Chat e
  browser-execution funcionam sem isso.
- **Sessões web** invalidam por IP/fingerprint — re-login é passo manual da migração,
  não bug (ver docs/napkin-lessons.md).
- **X/Twitter:** cookie presente e `/home` carrega, mas a conta pode ter desafio de
  verificação disparado no login manual (lado do X).
- **Registry de watchers volátil:** restart zera; consumidores re-registram por alias.
