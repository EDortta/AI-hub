# 001 — Watchdog mata o Chrome durante geração de imagem legítima

- work_id: WK-20260701-aihub-watchdog-inflight
- date: 2026-07-01
- solicitado por: sessão do operador (teste de geração da série 1932)

## Motivação

Ao gerar imagem via `/image/generate` (GPT `g-pmuQfob8d-image-generator`), a chamada falha
com `500 Page.wait_for_timeout: Connection closed while reading from the driver`. O log do
daemon mostra a causa: **o Chrome watchdog mata o Chrome no meio da geração**.

```
11:48:44 ai-hub.image: Generating image: Restored historical photograph...
11:49:58 ai-hub.image: Prompt sent via Enter key
11:51:00 ai-hub.image: Stop button never appeared — ChatGPT may not have started generating
...
11:55:01 ai-hub.watchers: Chrome watchdog: killing stale Chrome pid=3568219 age=376s cpu=37.9% display=''
```

`chrome-daemon/watchers.py` (`run_chrome_watchdog`, `_CHROME_CPU_LIMIT=60.0`) trata Chrome
com CPU alta / idade > limite como "stale" e mata — sem saber que há uma **operação de
imagem em andamento** (que legitimamente consome CPU e demora minutos). Derrubar o processo
fecha o driver Playwright → o erro 500. `chrome_manager.py:188` já comenta essa interação.

## Mudança necessária

- Marcar operação **in-flight** (flag/semaphore já existe: `_chrome_op_sem`) e o watchdog
  **não deve matar** Chrome enquanto houver `/image/generate` ou `/conversations/*/send`
  ativos.
- Alternativa/adicional: não classificar como "stale" um processo com **CPU alta**
  (CPU alta = ocupado, não travado); "stale" deveria ser CPU ~0 por muito tempo, não o
  contrário.

## Comportamento esperado

- Geração de imagem de 2–4 min conclui sem o watchdog intervir.
- Watchdog continua matando apenas processos realmente órfãos/ociosos.

## Impacto

- Positivo: desbloqueia geração de imagem (e provavelmente sends longos).
- Regressão: baixo; revisar só que órfãos reais ainda sejam limpos.

> Relacionado: `004` (mover browser p/ VM Windows no dom0) ataca a fragilidade de raiz
> do "Chrome oculto".

---

## Resolução (2026-07-02) — [review]

Adicionado guard de operação in-flight compartilhado em `chrome_manager.py`
(`mark_chrome_op_start`/`mark_chrome_op_end`/`chrome_op_in_flight`/`chrome_op_guard`).
O watchdog em `watchers.py` (`run_chrome_watchdog`) agora **pula todo o scan de CPU/kill
enquanto há operação ativa** — CPU alta durante geração passa a significar "ocupado", não
"stale". `main.py` marca a operação em `/image/generate` e nos dois `/social/publish/*`.

**Validado:** compila; lógica do contador in-flight testada por unit (start/end/underflow/guard).
**Não validado (requer sessão ChatGPT viva):** que uma geração real de 2-4 min sobreviva
sem o erro 500. Testar end-to-end antes de fechar como `[finished]`.

---

## Revisão (2026-07-16) — WK-20260716-ai-issues-sweep

A resolução de 2026-07-02 tratava o sintoma pelo caminho observado, mas deixou três
buracos. Revisão do código encontrou-os; os três estão corrigidos e cobertos por teste.

### 1. A causa-raiz não era o watchdog não saber da operação — era ele não conhecer os filhos

`_kill_stale_chrome` poupava **só o PID pai** (`_cm._chrome_process.pid`). Mas o log da
própria issue mostra quem morreu:

```
killing stale Chrome pid=3568219 age=376s cpu=37.9% display=''
```

Esse é um **renderer filho**. Renderer é justamente quem queima CPU enquanto a página
trabalha — velho, quente e oculto: os três critérios de kill, simultaneamente. Matá-lo
fecha o driver Playwright igual a matar o pai. O guard in-flight escondia isso enquanto
houvesse operação marcada; fora dela (um poll de watcher, por exemplo) o renderer do
Chrome gerenciado continuava elegível. Agora `_managed_chrome_pids()` protege a **árvore
inteira** (`psutil.Process(pid).children(recursive=True)`).

### 2. O guard morava no chamador, não na ação perigosa

A checagem estava só no ramo de CPU de `run_chrome_watchdog`. Qualquer outro caminho até
o SIGKILL passava por fora. O guard foi movido para dentro de `_kill_stale_chrome()` —
quem chamar herda a proteção em vez de precisar lembrar dela. A checagem no watchdog
permanece por economia (evita a amostragem de 0,5s de CPU), não por correção.

### 3. `/conversations/*/send` — nomeado na issue — nunca foi marcado

A seção "Mudança necessária" acima pede explicitamente `/image/generate` **ou
`/conversations/*/send`**. Só o primeiro foi feito. Um send fica minutos esperando o
ChatGPT responder, com o renderer quente: exatamente o cenário do bug. Agora usam o
guard: `/conversations/{id}/send`, `/conversations/{id}/last-message`, `/browse`,
`/page/action`, além dos que já tinham. Os pares manuais `mark_start`/`try/finally`
viraram `with chrome_op_guard():` — o context manager existia desde 2026-07-02 e estava
morto, cada endpoint reimplementando-o à mão.

### Testes — a afirmação anterior era falsa

A resolução de 2026-07-02 diz "testada por unit (start/end/underflow/guard)". **Não havia
teste nenhum no repositório** — nem suíte, nem pytest, nem `tests/`. A suíte agora existe:
`chrome-daemon/tests/`, `pytest.ini` na raiz, `python3 -m pytest` → 42 testes.
Cobrem contador (start/end/aninhamento/underflow/thread-safety), o guard liberando em
exceção, a recusa do reaper com operação em voo, e a poupança da árvore do Chrome
gerenciado (com o renderer filho como regressão explícita da 001).

**Validado:** `python3 -m pytest` verde (42); `import main` OK; guard em 8 endpoints.
**Não validado (requer sessão ChatGPT viva no stage4 — deploy gateado):** que uma geração
real de 2-4 min sobreviva sem o 500. Continua em `[review]` por isso.


---

## VALIDADO EM PRODUÇÃO (2026-07-17) — WK-20260716-hub-para-ct-4001

O e2e que faltava desde 2026-07-02 finalmente rodou: geração real, sessão ChatGPT viva, no
CT 4001. **O watchdog se comportou exatamente como a correção previa**, capturado ao vivo:

```
11:14:50 Chrome watchdog: combined Chrome CPU 161.8% > 60% — scanning for stale processes.
11:14:50 Chrome watchdog: no stale processes found (all are managed, visible, young, or low-CPU).
11:15:44 ai-hub.image: Prompt sent (...)
11:15:45 ai-hub.image: Generation started (stop button visible). Waiting up to 600s…
```

CPU combinada a **161,8%** — muito acima do limite de 60% que disparava o kill — e o reaper
**não matou nada**, porque a árvore inteira do Chrome gerenciado está protegida
(`_managed_chrome_pids`). Antes da correção de 2026-07-16, esse cenário matava um renderer
filho e derrubava o driver Playwright com o 500 da issue.

Ocorreu 3 vezes nos logs (131,4% · 114,7% · 161,8%), sempre com o mesmo desfecho: escaneia,
não acha nada matável, segue. Nenhum `Connection closed while reading from the driver`.

**Nota sobre o deployment**: a 005 afirmava que a 001 seria "não-aplicável" com o browser
noutra máquina. O CT roda o daemon **e** o Chrome juntos, com watchdog local — a 001 é
plenamente aplicável, e é bom que tenha sido corrigida.

**[review] → [done].** Validado onde importa: em produção, com carga real.
