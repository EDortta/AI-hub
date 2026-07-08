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
