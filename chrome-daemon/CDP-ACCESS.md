# Acesso ao Chrome do AI-hub via CDP (receita + armadilhas)

Aprendido em 2026-06-29 ao criar um evento no Google Calendar pelo Chrome logado.

## O ambiente

- O `chrome-daemon` é o **dono** de um Chrome `--headless=new` com a conta Google logada.
- Perfil: `~/.local/share/ai-hub/chrome-profile`.
- CDP exposto em `http://127.0.0.1:9222` (porta `CDP_PORT` em `chrome_manager.py`).

## ⚠️ NÃO use um 2º cliente Playwright `connect_over_cdp`

O daemon já mantém uma conexão Playwright (`sync_playwright().chromium.connect_over_cdp`)
no **nível do browser**. Abrir uma segunda conexão Playwright concorrente **trava** —
`connect_over_cdp` tenta anexar a todos os targets e estoura `TimeoutError` (180s),
mesmo com o websocket conectado. Não dá pra ter dois donos do mesmo browser-endpoint.

## ✅ Receita: dirigir UMA aba isolada por CDP cru

Não toca no browser-endpoint do daemon; abre/controla só uma página.

1. **Abrir aba já navegada:** `PUT http://127.0.0.1:9222/json/new?<URL-encodada>`
   → retorna JSON com `id` e `webSocketDebuggerUrl`.
2. **Conectar no websocket da aba** com `websocket-client` e
   **`suppress_origin=True`** (obrigatório):
   o Chrome 148 rejeita o handshake com header `Origin` →
   `403 Forbidden ... Use --remote-allow-origins`. Suprimir o Origin passa.
3. Enviar `Page.enable` e `Runtime.enable`. Usar
   `Runtime.evaluate` com `returnByValue=True, awaitPromise=True` para:
   - ler `location.href` / `document.readyState`,
   - detectar redirect de login (`accounts.google.com`),
   - clicar elementos por `aria-label`/`textContent` (ex.: botão **Save**/**Salvar** —
     a UI pode estar em inglês mesmo com conta pt-BR).
4. **Limpar:** fechar a aba com `GET http://127.0.0.1:9222/json/close/<id>`.

Protocolo CDP por aba: enviar `{"id":N,"method":...,"params":...}`; respostas casam
por `id` (eventos chegam sem `id`).

## Exemplo aplicado

Google Calendar: abrir
`https://calendar.google.com/calendar/u/0/r/eventedit?text=<titulo>&dates=YYYYMMDD/YYYYMMDD&details=<desc>`
(o range com 2 datas = evento de dia inteiro), depois clicar **Save**. Sucesso =
a URL sai de `/eventedit` e volta para `/calendar/u/0/r`.

Script de referência usado: ver histórico da sessão (scratchpad `cal_cdp.py`).

## ⚠️ Modelo de ameaça do CDP (SEC-0036)

O protocolo CDP não tem autenticação própria: quem alcança `127.0.0.1:9222`
controla o Chrome inteiro (cookies, JS, abas) do perfil autenticado. Duas
camadas de contenção existem hoje, e uma limitação que **permanece**:

- **Bind em loopback** — a porta só é alcançável por processos no mesmo host
  (não é exposta na rede).
- **`--remote-allow-origins=`** (explícito em `chrome_manager.py`) — o Chrome
  rejeita handshakes de WebSocket que carreguem header `Origin`, ou seja,
  qualquer página web tentando alcançar o CDP via DNS-rebinding é barrada com
  `403 Forbidden`. É por isso que scripts de CDP cru (receita acima) precisam
  de `suppress_origin=True`: eles não são páginas web, então não têm Origin a
  suprimir de verdade, mas o parâmetro evita que o client injete um.
- **Limitação que fica:** qualquer outro processo local (outro usuário do
  mesmo host, ou qualquer programa rodando como o mesmo usuário) que não seja
  uma página de browser ainda consegue conectar sem Origin e assumir o
  controle total — o bind em loopback não distingue processos "amigos" de
  "hostis" no mesmo host. Mitigar isso de verdade exigiria trocar
  `--remote-debugging-port` por `--remote-debugging-pipe` (CDP via pipe, sem
  socket TCP) — mudança arquitetural maior, fora do escopo deste lote de
  correções. Até lá: **não rode este daemon em host multiusuário**, e trate
  qualquer processo capaz de rodar como este usuário como equivalente a ter
  acesso total às contas logadas no Chrome do daemon.
