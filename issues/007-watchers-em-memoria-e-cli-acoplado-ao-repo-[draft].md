# 007 — Watchers em memória e CLI acoplado ao repositório

- work_id: `WK-20260709-ai-namespace-migration`
- date: 2026-07-09
- tipo: **dívida técnica** (dois itens independentes, ambos expostos pela migração)
- status: draft

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
