# 007 — Watchers em memória e CLI acoplado ao repositório

- work_id: `WK-20260709-ai-namespace-migration`
- date: 2026-07-09
- tipo: **dívida técnica** (dois itens independentes, ambos expostos pela migração)
- status: open — parte 1 resolvida (2026-07-16); parte 2 implementada em
  `feature/007-cli-packaging` (2026-08-26, WK-20260826-007-cli-packaging)

## 1. `WatcherRegistry` é volátil

`chrome-daemon/watchers.py` guarda os watchers num dicionário em memória (`WatcherRegistry._watchers`).
Não há persistência. **Um restart do `chrome-daemon` apaga todas as conversas registradas.**

Consequências já observadas:

- O `RESUME.md` deste repo descreve o watcher `Claudia` sumindo de `/conversations` — é isto.
- Qualquer consumidor que guarde `watcher_id` como estado durável passa a apontar para um watcher morto,
  silenciosamente, depois de um restart.
- O guardião (`~/scripts/ai-hub-guardian.sh`, cron 5 em 5 min) **reinicia o daemon** após 3 falhas
  consecutivas. Ou seja, o apagamento é automático e recorrente, não excepcional.

Isso condiciona o desenho do lado do Gateway: qualquer coisa que resolva conversa deve resolver por
**alias** e re-registrar sob demanda (é o que `aihub_driver._resolve_watcher_id` já faz), nunca confiar
num `watcher_id` guardado.

**Proposta**: persistir o registry em disco (JSON em `~/.local/share/ai-hub/`), recarregar no boot, e
reconciliar contra as abas realmente abertas no Chrome. É independente da convergência de API e vale por si.

### Resolução (2026-07-16) — WK-20260716-ai-issues-sweep

Persistência implementada em `chrome-daemon/watchers.py`, com o registry **sem conhecer o formato
de armazenamento** (inversão de dependência — trocar JSON por sqlite é uma classe nova, não uma
edição no registry):

- `WatcherStore` (interface) · `NullWatcherStore` (não persiste — o comportamento histórico,
  agora uma escolha explícita e o default do construtor) · `JsonFileWatcherStore`
  (`~/.local/share/ai-hub/watchers.json`).
- Escrita **atômica** (`tmp` + `os.replace`) e `chmod 600`. Um crash no meio da escrita deixa o
  arquivo bom anterior, nunca um truncado — que leria como "nenhum watcher" no boot seguinte.
- `registry.restore()` no lifespan; `registry.checkpoint()` a cada 30s no loop de polling
  (no-op quando nada mudou — daemon ocioso não reescreve o arquivo) e no shutdown limpo.
- Arquivo corrompido → loga e **sobe vazio**. Registry vazio se recupera com re-registro;
  crash loop no boot, não.

**O que é persistido e o que não é — decisão consciente:**

- `seen_hashes` **é** persistido (md5, unidirecional, sem conteúdo). Sem ele, um watcher restaurado
  relê a conversa inteira e roteia **toda mensagem antiga para o inbox como se fosse nova** — o
  restart trocaria "sumiu" por "avalanche". Foi o achado de desenho mais importante desta parte.
- `inbox` **não** é persistido. Guarda texto verbatim do ChatGPT; gravá-lo transformaria uma fila
  transitória em **conteúdo de conversa em repouso** — decisão de dados que esta issue não tem
  mandato para tomar. Se o operador quiser inbox durável, é decisão separada (com retenção).

**Reconciliação contra as abas do Chrome**: não foi feita explicitamente, e por ora não precisa —
`get_or_open_page(w.url)` no poll já reabre a aba se ela não existir. Um watcher restaurado cuja
conversa foi apagada no ChatGPT vai falhar no poll e logar; não trava o daemon.

**Validado:** 14 testes em `chrome-daemon/tests/test_watcher_registry.py` — round-trip,
unregister persistido, `seen_hashes` preservado, inbox ausente do disco, arquivo corrompido,
entrada inválida, permissão 600, falha de escrita não derruba o registro. Um bug real foi pego
pelo próprio teste: entrada corrompida virava watcher válido com id novo e URL vazia (todos os
campos do dataclass têm default) — agora `id`/`url` são obrigatórios na desserialização.
**Não validado:** restart real do daemon no stage4 (deploy gateado).

> Parte 1 **resolvida**. A issue segue aberta pela **parte 2** (empacotar o CLI), abaixo.

## 2. O CLI `ai-hub` é um symlink para dentro do repositório

`install/install.sh` instala `~/.local/bin/ai-hub` como link para `chrome-daemon/cli.py`. Depois da
migração de 2026-07-09 o link é **relativo** (`../../Sync/Projects/AI/hub/chrome-daemon/cli.py`) e o
`install.sh` passou a gerá-lo com `ln -sfr`, além de derivar o `ExecStart` da unit a partir de `$DAEMON_DIR`
em vez de embutir o caminho. Isso removeu a regressão, mas não a natureza do acoplamento: o binário no
`PATH` continua apontando para dentro de um checkout.

**Proposta**: empacotar o `chrome-daemon` com `console_scripts` (`ai-hub = ai_hub.cli:main`) e instalar por
`pipx`. O CLI vira um binário de verdade e o `PATH` deixa de conhecer `~/Sync/Projects`.

Distinção que importa: **operador rodando o CLI no host do Hub é legítimo.** O que a regra proíbe é um
*projeto consumidor* referenciar a pasta do Hub — caso do `GestaoContasFernanda`, tratado em
`GestaoContasFernanda/issues/ARCH-20260709-*`.

Enquanto o pacote não existir, os endpoints operacionais (`setup`, `login-done`, `logs`, `session/*`) devem
continuar como CLI local, **não** atrás do Gateway: expô-los daria a qualquer projeto o poder de reiniciar
a sessão de browser de outro.

### Resolução (2026-08-26) — WK-20260826-007-cli-packaging

Implementada em `feature/007-cli-packaging`:

- `cli.py` e `client.py` viraram o pacote **`ai_hub`** (`chrome-daemon/ai_hub/`), com
  `chrome-daemon/pyproject.toml` declarando `[project.scripts] ai-hub = "ai_hub.cli:main"`.
  O hack de `sys.path` do cli.py foi removido; o import agora é `from ai_hub.client import …`.
- **Escopo deliberado**: só `ai_hub` (CLI + cliente) é empacotado. O daemon (`main.py`,
  `watchers.py`, …) continua rodando do checkout via systemd, com `requirements.txt` próprio —
  a venv do pipx recebe apenas `httpx` + `pyyaml`, nunca fastapi/uvicorn/playwright.
- `install/install.sh`: exige `pipx` no início quando o CLI faz parte da execução
  (fail-fast), remove o symlink legado `~/.local/bin/ai-hub` se existir e roda
  `pipx install --force "$DAEMON_DIR"`. O `PATH` deixa de conhecer `~/Sync/Projects`.
  Ganhou dois modos vindos da crítica pré-commit: `--cli-only` (só o pipx do CLI —
  migrar/atualizar o CLI sem mexer no serviço do daemon) e `--skip-cli` (só daemon,
  sem exigir pipx).
- **Rollout no host real (passo obrigatório, no mesmo deploy):** o `git pull` que traz
  esta mudança apaga `chrome-daemon/cli.py` e deixa o symlink antigo pendurado — `ai-hub`
  fica ENOENT até rodar `install.sh --cli-only`. E como o pipx congela o código na venv,
  todo deploy futuro que mudar `ai_hub/` precisa repetir `--cli-only`, senão CLI e daemon
  divergem silenciosamente.
- Os endpoints operacionais (`setup`, `login-done`, `logs`, `session/*`) **permanecem
  CLI-local**, não atrás do Gateway — a distinção acima continua valendo; empacotar não
  mudou a superfície exposta.
- Docs atualizadas: `README.md` (instalação via pipx, re-instalar após mudar `ai_hub/`) e
  `docs/INTEGRATION.md` (cliente Python importa `ai_hub.client` em vez de "copie client.py").

**Validado:** suíte completa verde (87 testes, 5 novos em `tests/test_cli_packaging.py`:
entry point do pyproject resolve para callable; import do CLI não puxa dependências do
daemon — verificado em subprocess; `AIHubClient` importável como módulo do pacote;
`main()` sem comando imprime help e retorna 1; pyproject empacota só `ai_hub`).
Instalação real exercitada em venv isolada: `pip install chrome-daemon/` → `ai-hub --help`
funciona, `ai-hub status` sem daemon falha limpo, `Requires: httpx, pyyaml`.

**Não validado (gateado):** rodar `install.sh` no host real do Hub (mexe em systemd e no
`~/.local/bin` do operador) e o restart do daemon — deploy é ação aprovada pelo operador,
não autônoma.

**Concílio (2 rodadas, 2026-08-26) — contagens:**

- **Levantados:** 13 — 7 na crítica pré-commit (3 técnica: README com comando ambíguo
  que resolveria no PyPI, PEP 668 no comando do INTEGRATION.md, symlink pendurado entre
  o pull e o install; 4 cética: janela de deploy sem passo leve de migração, remediação
  de pipx impressa que falha em Debian 12, cli.py convidando execução direta quebrada,
  drift silencioso do CLI congelado na venv) + 6 no concílio R1 (1 técnica: argumentos
  extras ignorados; 5 cética: build/ in-tree contaminando wheels futuros, sdist com
  testes que não rodam + IP interno no METADATA, `--skip-cli` deixando symlink pendurado
  em silêncio, `pipx --force` reaproveitando venv homônima com sobras, egress/no-restart
  não documentados). Segurança: zero achados nas duas rodadas.
- **Sobreviveram após a R2:** 0 — verificador confirmou os 6 itens da R1 fechados, com
  reprodução empírica dos triggers (wheel contaminado por build/ estale, symlink
  pendurado, args inválidos), e nenhum achado novo introduzido pelas correções.
- **Viraram teste:** 1 — `test_sdist_hygiene_stays_pinned` pina o achado A2 (pyproject
  sem readme; MANIFEST.in mantendo prune de tests/docs/install). Suíte: 88.
- **Perguntas abertas:** 1 — pré-existente, fora do escopo desta branch: o nginx `:9480`
  documentado em `INTEGRATION.md` alcança `/session/login` e `/session/login-done` com o
  token compartilhado; se a política "endpoint operacional = só CLI local" deve valer
  também para o proxy nginx (não só para o Gateway), merece issue própria — decisão do
  operador.
