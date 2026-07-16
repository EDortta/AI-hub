# Handoff — chrome-daemon

## Sessão 2026-07-16 (WK-20260716-ai-issues-sweep) — issues 001/002/003/007-p1

Varredura das issues abertas: implementar, criticar, corrigir. **Nada foi implantado** —
o daemon no stage4 continua rodando o código anterior. Deploy é passo seu.

- **Suíte de testes criada do zero** (o repo não tinha nenhuma): `chrome-daemon/tests/`,
  `pytest.ini` na raiz. `cd ~/Sync/Projects/AI/hub && python3 -m pytest` → **42 testes**.
- **001** — causa-raiz corrigida, não o sintoma: `_kill_stale_chrome` poupava só o PID pai;
  quem o log da issue mostra morrendo é um **renderer filho**. `_managed_chrome_pids()`
  agora protege a árvore. Guard movido para **dentro** do reaper. `/conversations/*/send`
  (nomeado na issue, nunca marcado) e mais 3 endpoints agora usam `chrome_op_guard()`.
- **002** — detecção de perda de contexto extraída para `is_chatgpt_home()`, pura e testada.
- **003** — revisada; guard consistente. Seletores continuam não testáveis fora do browser.
- **007 parte 1** — registry **persistido**: `WatcherStore`/`NullWatcherStore`/
  `JsonFileWatcherStore` (`~/.local/share/ai-hub/watchers.json`, atômico, chmod 600).
  `restore()` no boot, `checkpoint()` a cada 30s. `seen_hashes` persiste; **inbox não**.
- **004 e 005** → `[superseded]`: queriam browser em VM dedicada sempre-ligada — o stage4
  já entregou isso em 2026-07-15.
- **007 parte 2** (empacotar CLI com pipx) — **adiada por decisão sua**: exige renomear
  módulos e reescrever o install.sh num daemon em produção que não posso testar e2e.

### Próximo passo (DO THIS FIRST)
Nada é urgente. Quando quiser fechar 001/002/003 como `[finished]`, o que falta é **e2e com
sessão ChatGPT viva no stage4** — uma geração de 2-4 min que sobreviva sem o 500, e os
seletores do delete. Ao implantar, note que o `watchers.json` **muda comportamento no boot**:
watchers registrados voltam depois do restart (era o bug 007; agora é a feature).


## Topologia atual (desde 2026-07-16, WK-20260716-hub-para-ct-4001)

O chrome-daemon **roda no CT 4001 `ai-ecosystem`** do stage4 — **não** na baremetal.

- **stage4 é o HOST Proxmox** (`/etc/pve` presente), `192.168.7.200`. A frase anterior aqui
  ("VM Proxmox Debian 12") **estava errada** e induziu a desenhar em cima de ficção: não há
  VM; o daemon estava direto no hypervisor.
- **CT 4001**: Debian 12, `unprivileged: 1`, `features: nesting=1`, 3G/2 cores, **rede
  interna `vmbr2` `192.168.1.5`** — fora da LAN.
- **Chrome com sandbox completo** dentro do CT unprivileged (userns/pidns/netns do renderer
  isolados, chroot, seccomp 2). **Nunca** `--no-sandbox`.
- **API**: `0.0.0.0:9400` **dentro do CT** (`AIHUB_BIND_HOST`), exposta só pelo **nginx do
  host** em `:9480` (`proxy_pass http://192.168.1.5:9400`, `Host: localhost`). Porta única.
- **CDP** `127.0.0.1:9222` **do CT** — inalcançável do hypervisor e da LAN.
- **Acesso**: `ssh ai-ecosystem` (ProxyJump por `stage4-inovacao`) ou `pct enter 4001`.
- **Guardian** roda no host (precisa do `pct`); **process_monitor** roda dentro do CT.
- **Rollback**: perfil e checkout do `ai-hub` intactos no host; unit apenas `disable`.

> **PENDENTE: login do ChatGPT (2FA, do operador).** `logged_in: false`. Já estava falso no
> host antes da migração — é dívida antiga, não custo dela. Ver issue 008.

### Histórico: 2026-07-15 (WK-20260715-aihub-stage4)
O daemon migrou do devel3 para o stage4 — mas para a **baremetal** do hypervisor.

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

## Documentação de integração (servida pela API)
Guia único em `chrome-daemon/docs/INTEGRATION.md`, servido ao vivo pelo daemon
**sem token** (rotas públicas sob host-guard):
- `GET http://192.168.7.200:9480/llms.txt` — caminho canônico p/ agentes LLM.
- `GET http://192.168.7.200:9480/docs/integration.md` — mesmo conteúdo.
- `GET http://192.168.7.200:9480/` — índice apontando para os docs.
Para integrar um app: "Integre com o ai/hub, a documentação está em
http://192.168.7.200:9480/llms.txt". Editar o `.md` no repo + rsync + restart
atualiza o que a API serve (o endpoint lê o arquivo em disco a cada request).

## Pendências conhecidas
- **Sessões web** invalidam por IP/fingerprint — re-login é passo manual da migração,
  não bug (ver docs/napkin-lessons.md).
- **X/Twitter:** cookie presente e `/home` carrega, mas a conta pode ter desafio de
  verificação disparado no login manual (lado do X).
- **Registry de watchers volátil:** restart zera; consumidores re-registram por alias.
