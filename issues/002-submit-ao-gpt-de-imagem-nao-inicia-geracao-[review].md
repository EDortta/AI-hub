# 002 — Envio ao GPT de imagem não inicia a geração (página volta para chatgpt.com/)

- work_id: WK-20260701-aihub-submit-nao-inicia
- date: 2026-07-01
- solicitado por: sessão do operador (teste de geração da série 1932)

## Motivação

No `/image/generate`, o GPT carrega e o prompt é enviado, mas a geração **não começa**:

```
11:48:49 ai-hub.image: GPT page loaded: https://chatgpt.com/g/g-pmuQfob8d-image-generator
11:49:58 ai-hub.image: Prompt sent via Enter key
11:51:00 ai-hub.image: Stop button never appeared — ChatGPT may not have started generating
11:51:00 ai-hub.image: _wait_for_new_image start — before_srcs count=0
11:51:15 ai-hub.image: iter=2 page_url=https://chatgpt.com/  img_count=4   (travado)
```

Dois sintomas:
1. **"Stop button never appeared"** — o clique/Enter não disparou a geração (seletor de
   submit ou timing após load do GPT).
2. A `page_url` **deixa de ser o GPT e vira `https://chatgpt.com/`** — houve navegação/
   redirect que tirou o contexto do GPT; o `_wait_for_new_image` então observa a home
   errada e nunca acha imagem nova.

## Mudança necessária

- Confirmar que o composer do GPT está **pronto e focado** antes do envio; validar que o
  envio realmente criou um turno (aguardar aparecimento do stop button com retry/refill).
- **Fixar o contexto do GPT**: se a página sair da URL do GPT, reabrir/re-navegar em vez de
  seguir observando `chatgpt.com/`.
- Se após N tentativas a geração não iniciar, **falhar rápido** com erro claro
  (`generation_did_not_start`) em vez de poluir com polls de 700s.

## Comportamento esperado

- Prompt enviado → stop button aparece → nova imagem detectada e baixada, **na URL do GPT**.

## Impacto

- Positivo: geração de imagem passa a funcionar de forma determinística.
- Regressão: baixo; mudança no fluxo de submit/observação.

---

## Resolução (2026-07-02) — [review]

Em `image_generator.py`: `_fill_and_send` agora foca o composer (click) antes de `fill` e
antes do fallback `Enter`, para o envio ligar ao contexto do GPT correto. `_wait_for_done`
**falha rápido** com `generation_did_not_start` quando o stop button não aparece em 60s
(antes seguia poluindo ~600s em `_wait_for_new_image`), e detecta perda de contexto quando a
URL reverte para `chatgpt.com/` ou `chat.openai.com/`.

**Validado:** compila / AST.
**Não validado (requer sessão ChatGPT viva):** que o envio real dispare a geração e que a
detecção de perda de contexto acione no cenário observado. Testar end-to-end.

---

## Revisão (2026-07-16) — WK-20260716-ai-issues-sweep

A detecção de perda de contexto estava correta mas **enterrada dentro do loop de espera**
de `_wait_for_done`, misturada com o polling do stop button. Consequência prática: não
havia como testá-la sem um browser vivo — e é por isso que ela chegou até aqui como
"não validado".

Extraída para `is_chatgpt_home(url)`, função pura, agora coberta por teste
(`chrome-daemon/tests/test_image_generator.py`). A extração também consertou dois furos
do check original (`base = cur.split("?")[0].rstrip("/")`):

- **fragmento não era removido** — `https://chatgpt.com/#foo` não era reconhecido como home,
  e a espera seguiria os ~600s que a issue quer evitar;
- **`url` vazia/None** — `(page.url or "")` virava `""`, que não casa com nenhuma home e
  cai no erro genérico. Correto, mas por acidente; agora é explícito e testado.

Testado também o caso lookalike (`https://chatgpt.com.evil.net/` não é home) — o check é
sobre a URL base inteira, não substring.

**Validado:** `python3 -m pytest` verde; `is_chatgpt_home` coberta em 15 casos.
**Não validado (requer sessão ChatGPT viva no stage4 — deploy gateado):** que o envio real
dispare a geração e que o foco do composer resolva o sintoma observado. O `_fill_and_send`
depende de seletores da UI do ChatGPT e não é testável fora do browser. Continua `[review]`.
