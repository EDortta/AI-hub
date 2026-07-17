# 009 — GPT de imagem preso em "Thinking": geração inicia e nunca produz imagem

- work_id: `WK-20260717-gpt-thinking-nao-gera`
- date: 2026-07-17
- tipo: **integração / UI do ChatGPT**
- status: draft
- origem: e2e da issue 002 no CT 4001 (2026-07-17) — o envio funcionou, a imagem não veio

## Sintoma

Geração real via `POST /image/generate` no GPT `g-pmuQfob8d-image-generator`, com sessão viva:

```
11:15:44  Prompt sent (Hey, A simple red circle on a white background...)
11:15:45  Generation started (stop button visible). Waiting up to 600s…
11:16:xx  _wait_for_new_image start — before_srcs count=0
11:17:01  iter=4  page_url=.../g-pmuQfob8d-image-generator/c/6a5a0ede-...  img_count=4
...
11:21:47  iter=23 page_url=.../g-pmuQfob8d-image-generator/c/6a5a0ede-...  img_count=4
```

`img_count` **parado em 4** por >10 minutos. Nenhuma imagem nova.

## O que NÃO é (importante — três issues foram descartadas por medição)

- **Não é a 001** (watchdog): CPU chegou a 161,8% e o reaper não matou nada. Correto.
- **Não é a 002** (envio/contexto): o stop button apareceu em 1s e a `page_url` ficou no GPT
  o tempo todo, com id de conversa. Correto.
- **Não é sessão**: `/session/check` → `logged_in: true` antes e depois.
- **Não é rede**: o host e o CT com internet (13ms) após a correção da rota default.

## O que é — evidência visual

Screenshot da página durante a espera (`/debug/screenshot`):

- o prompt chegou íntegro: *"Hey, A simple red circle on a white background, minimalist, flat
  design — orientação square"*;
- a resposta do assistente está em **`Thinking`**, e fica lá;
- o **seletor de modelo do composer mostra `Thinking`** — um modelo de raciocínio, não o
  modelo padrão de geração;
- conta: `Esteban Dortta / Plus`.

**Hipótese:** o perfil foi recriado no re-login de 2026-07-17 e caiu no modelo **Thinking**
como default. Um modelo de raciocínio ou não aciona a ferramenta de imagem deste GPT, ou
demora muito além do razoável para um círculo vermelho.

## Investigar (ao retomar)

1. Trocar o modelo para o padrão (não-Thinking) **na UI**, via VNC, e repetir a geração.
   Se resolver, a causa é o default do perfil novo.
2. Se resolver: o daemon deve **fixar/validar o modelo** antes de enviar? Ou é config de
   perfil, feita uma vez no login? Decidir — mas registrar, porque um perfil recriado no
   futuro vai cair no mesmo buraco.
3. Se **não** resolver: o GPT `g-pmuQfob8d` pode ter mudado ou saído do ar. Testar geração de
   imagem nativa (sem GPT customizado) para isolar.

## Por que isto importa mais do que parece

O `_wait_for_new_image` **espera 600s em silêncio** e só então falha. A issue 002 já atacou
esse padrão no `_wait_for_done` (falha rápida com `generation_did_not_start`), mas o
`_wait_for_new_image` continua sendo um poll longo que não distingue "gerando devagar" de
"nunca vai gerar". Vale considerar: se o texto do assistente está em `Thinking` há N minutos
e `img_count` não mudou, é sinal de que não vem imagem — falhar com erro nomeado é melhor que
600s de log.

## Impacto

- **Bloqueia** a validação e2e da issue **003** (delete do chat pós-geração): sem imagem, o
  fluxo não chega ao delete.
- Bloqueia o uso real de `/image/generate` — que é uma das razões de o hub existir.
