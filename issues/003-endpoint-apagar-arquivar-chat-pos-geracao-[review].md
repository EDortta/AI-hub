# 003 — Endpoint para apagar/arquivar o chat após a geração

- work_id: WK-20260701-aihub-delete-chat
- date: 2026-07-01
- solicitado por: sessão do operador (fluxo da série 1932)

## Motivação

O fluxo pedido pelo operador é: **gerar a imagem → apagar o chat** (não deixar rastro no
histórico do ChatGPT). Hoje o daemon **não tem como apagar a conversa**:

- `/image/generate` navega ao GPT, gera e baixa; só fecha a aba antiga na **próxima**
  chamada. A conversa permanece no histórico do ChatGPT.
- `DELETE /conversations/{watcher_id}` só **desregistra o watcher** (estado local), não
  apaga o chat no ChatGPT.

Consequência real: os testes desta sessão deixaram chats órfãos no histórico.

## Mudança necessária

- Novo endpoint (ex.: `POST /image/generate` com `delete_chat: true`, ou
  `POST /page/delete-chat`) que, após baixar a imagem, execute a ação de **apagar/arquivar
  a conversa** na UI do ChatGPT (menu da conversa → Delete → confirmar).
- Idempotente e best-effort: se a UI mudar, logar e não derrubar a geração.

## Comportamento esperado

- `generate_image(..., delete_chat=True)` → imagem salva **e** conversa removida do
  histórico.

## Impacto

- Positivo: fecha o ciclo "gerar e não deixar rastro"; evita acúmulo de chats.
- Regressão: baixo; ação isolada pós-download.

---

## Resolução (2026-07-02) — [review]

Adicionado `delete_current_chat(page)` best-effort em `image_generator.py` (abre menu de
opções → Delete → confirma; log-and-continue, nunca derruba a geração). `/image/generate`
ganhou flag `delete_chat: bool = false` (apaga após o download da imagem). Novo endpoint
`POST /page/delete-chat {url}` para apagar sob demanda; retorna `{ok, deleted}`.

**Validado:** compila / AST.
**Não validado (UI frágil, requer sessão ChatGPT viva):** seletores do menu Delete/confirm.
Ajustar seletores conforme a UI real antes de fechar como `[finished]`.

---

## Revisão (2026-07-16) — WK-20260716-ai-issues-sweep

Implementação revisada e correta quanto ao contrato: best-effort, log-and-continue, nunca
derruba a geração; `delete_chat` é aditivo (default `false`), então nenhum cliente atual
muda de comportamento.

Uma correção aplicada: `/page/delete-chat` **não marcava a operação in-flight** de forma
consistente com os demais endpoints de Chrome — agora usa `chrome_op_guard()` como todos
(ver issue 001). Sem isso, um delete concorrente com uma geração podia ver o watchdog
reaproveitar o renderer.

Nada aqui é testável fora do browser: `delete_current_chat` é inteiramente seletores da UI
do ChatGPT (menu → Delete → confirm). Testar seletores contra mocks provaria apenas que o
mock combina com o código — não que a UI do ChatGPT combina. Por isso **não foi criado
teste de unidade para esta issue**; a validação honesta é o e2e com sessão viva.

**Validado:** compila; guard consistente com os demais endpoints.
**Não validado (requer sessão ChatGPT viva no stage4 — deploy gateado):** os seletores.
Continua `[review]`. Risco baixo por desenho: se os seletores não casarem, retorna
`deleted: false` e a imagem continua sendo entregue.
