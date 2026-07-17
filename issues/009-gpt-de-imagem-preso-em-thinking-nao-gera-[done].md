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

`img_count` **parado em 4** por >10 minutos. Nenhuma imagem nova. Desfecho após ~11min:

```json
{"detail":"Nenhuma imagem nova apareceu dentro do tempo limite."}
```
(início 08:14:56 · fim 08:26:18 — o `_wait_for_new_image` esgotou os 600s)

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


---

## CAUSA CONFIRMADA (2026-07-17) — era o modelo "Thinking"

O operador abriu a tela (`scripts/aihub-vnc.sh`), **tirou o modo Thinking**, e o GPT
**começou a gerar imediatamente**. Screenshot durante: o placeholder de renderização
("One last tweak…" + matriz de pontos) e o botão de stop ativo — exatamente o que não
acontecia em 11 minutos com o Thinking ligado.

A hipótese estava certa: o perfil recriado no re-login de 2026-07-17 caiu no modelo de
raciocínio como default, e esse modelo **não aciona a ferramenta de imagem** deste GPT (ou
demora além de qualquer limite razoável). Nada de código nosso estava errado.

## O que resta decidir — e é o que importa desta issue

Consertar na mão resolve **hoje**. A pergunta é o que impede a recorrência, porque
**todo re-login recria o perfil** e pode cair no mesmo default:

- **Opção A — deixar como config de perfil.** O modelo é escolha da UI, persistida no
  perfil do Chrome. Basta lembrar de conferir depois de cada re-login. *Custo:* depende de
  memória humana — e esta issue existe porque isso falhou uma vez.
- **Opção B — o daemon valida antes de enviar.** Ler o seletor de modelo antes do
  `_fill_and_send`; se estiver num modelo de raciocínio, falhar rápido com erro nomeado
  (`wrong_model_selected`) em vez de esperar 600s. Não corrige, mas **diagnostica em 2s** em
  vez de esconder atrás de "nenhuma imagem apareceu".
- **Opção C — o daemon fixa o modelo.** Clicar no seletor e escolher o modelo certo antes de
  enviar. Mais robusto e mais frágil ao mesmo tempo: é mais seletor de UI do ChatGPT para
  quebrar.

**Recomendação: B.** O padrão que a issue 002 já estabeleceu neste código é *falhar rápido
com erro nomeado em vez de poll longo e silencioso* — e o `_wait_for_new_image` é o último
lugar que ainda espera 600s sem distinguir "gerando devagar" de "nunca vai gerar". B aplica
a mesma lição, sem adicionar dependência de seletor de UI para *escrever* (só para ler).

## Achado lateral (registrar, não agir)

A tela mostra: **"New version of GPT available — Continue chatting to use the old version, or
start a new chat for the latest version."** O `g-pmuQfob8d` foi atualizado. O daemon sempre
navega para a URL do GPT e reusa a aba, então pode estar preso à versão antiga da conversa.
Não é o bug desta issue, mas explica divergência futura entre "o que vejo na UI" e "o que o
daemon obtém". Vale uma issue própria se aparecer sintoma.


---

## IMPLEMENTADO (2026-07-17) — Opção B, aprovada pelo operador

`image_generator.py`: antes de enviar o prompt, lê o rótulo de modo do composer
(`_composer_mode_label`) e, se for um modo de raciocínio (`is_reasoning_mode`), **falha em
~2s** com `wrong_model_selected` — a mensagem já manda rodar `scripts/aihub-vnc.sh` para
trocar o modelo. Antes: 600s de silêncio e "nenhuma imagem apareceu", que mandava quem lê
procurar no lugar errado.

**Falha aberta de propósito** (design-standards §6): se a UI mudar e não dermos conta de ler
o rótulo, a geração segue. É diagnóstico, não controle — bloquear por não conseguir ler
trocaria um problema raro por um permanente. O chip de modo só existe no DOM quando um modo
não-padrão está escolhido, então "sem rótulo" = modelo padrão = o caso bom.

Testes: 11 casos em `test_image_generator.py` (detecção de Thinking/Reasoning/Raciocínio;
default não-sinalizado; fronteira de palavra — "rethinking" não é modo; composer ilegível
falha aberto).

**Validado:** o classificador e o caso negativo (default, ao vivo — o composer não tem
rótulo). **Não validado ao vivo:** o caminho positivo (Thinking ligado disparando o erro) —
reproduzi-lo exigiria religar o Thinking pela VNC; a decisão do operador (2026-07-17) foi
seguir sem isso. A lógica está coberta por unit.

**[draft] → [done].** Decisão do operador: ficar com a versão antiga do GPT (o daemon já
reusa a aba, sem ação).
