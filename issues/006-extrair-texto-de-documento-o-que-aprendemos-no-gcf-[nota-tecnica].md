# 006 — Extrair texto de documento: o que aprendemos no GCF

- work_id: WK-20260709-comprovante-para-markdown
- date: 2026-07-09
- tipo: **nota técnica** (não é um bug do AI-Hub; é conhecimento transferível)
- origem: `GestaoContasFernanda`, conversão de comprovantes para markdown
- referência: `GestaoContasFernanda/docs/contratos/markitdown-ocr.md`

## Por que isto está aqui

O AI-Hub é a porta de entrada de conteúdo para vários projetos. Sempre que alguém for
transformar um PDF ou uma imagem em texto para alimentar um modelo, vai tropeçar nas
mesmas cinco pedras que o GCF levou sete semanas para enxergar. Este documento existe
para que o AI-Hub — e quem escrever fluxo de OCR/extração aqui dentro — não repita.

Nenhuma dessas lições é sobre comprovantes. Todas são sobre **o estágio de conversão**,
que quase sempre é tratado como detalhe e quase sempre é onde o erro nasce.

---

## Lição 1 — "Texto não vazio" não é "texto útil"

O GCF tinha uma cascata: extrai localmente; se veio texto, usa; se não veio nada, chama a
LLM. O `se não veio nada` era literalmente `if text.strip()`.

Consequência: OCR devolvendo ruído, ou uma camada de texto corrompida, contavam como
sucesso. A LLM **nunca** era chamada nos casos em que ela era exatamente o que faltava.

O conserto é um predicado explícito de utilidade, aplicado a **toda** saída de extração —
inclusive à resposta da LLM:

```
usable = todos:
  ≥ 40 caracteres não-brancos
  sem codepoints em U+E000–U+F8FF        (fonte subsetada sem ToUnicode válido)
  sem "(cid:NN)"                          (pdfminer sem mapeamento nenhum)
  sem U+FFFD                              (decodificação perdida)
  razão alfanumérica ≥ 0.5                (separa texto de ruído)
  ao menos um dígito                      (documento sempre tem número)
```

**Regra geral:** presença de saída não é evidência de acerto. Todo extrator precisa de um
portão, e o portão precisa ser função pura e testável.

## Lição 2 — Dependência opcional ausente + `except: pass` = código morto invisível

`markitdown` estava instalado **sem o extra `[pdf]`**. `MarkItDown().convert(pdf)` lançava
`MissingDependencyException` em 19 de 19 arquivos. Um `except: pass` engolia o erro e caía
no pdfminer, que funcionava. O pipeline rodou 7 semanas com o primeiro estágio morto, e o
contrato escrito descrevia um comportamento que nunca existiu.

Duas causas, ambas relevantes para o AI-Hub:

- **Nunca engolir exceção de import/dependência.** Logue no stderr do subprocesso e
  propague ao log do processo pai. Uma dependência que some tem que doer.
- **Fixe dependências de subprocesso num arquivo, não na prosa do contrato.** Se as libs
  Python de um script só existem descritas num `.md`, ninguém percebe quando faltam.

> Nota específica do markitdown: para PDF, o conversor dele é um wrapper de
> `pdfminer.high_level.extract_text`. Não espere markdown estruturado — o que sai é texto
> plano. Se você precisa de estrutura, ela é sua responsabilidade.

## Lição 3 — Um PDF pode ter camada de texto **e** estar corrompido

Este foi o achado que explicou o bug real. Apps de banco (PicPay, Sicredi, Mercado Pago)
embutem fontes subsetadas cujo `ToUnicode` aponta para a Private Use Area:

```
extraído:  15/mai/2026 - 22<U+E092>06<U+E092>44    44.863.959/0001<U+E088>26
correto:   15/mai/2026 - 22:06:44                  44.863.959/0001-26
```

Os codepoints PUA são **invisíveis no terminal**, então o texto parece apenas ter perdido
a pontuação. No corpus do GCF, **14 de 19** documentos estavam assim. Datas, CNPJ e IDs de
transação chegavam destruídos ao parser, sem nenhum sinal no log.

O caractere se perde, mas o **glifo** continua certo: nessas fontes o codepoint PUA é um
glifo *composto* que referencia o glifo real. Decompor com `fontTools` recupera o
caractere exatamente:

```python
glyph = ttfont['glyf'][glyph_name]
if glyph.isComposite():
    components = sorted(glyph.components, key=lambda c: c.y)
    if len(components) == 1:
        char = fontTools.agl.toUnicode(components[0].glyphName)   # uniE088 → hyphen → '-'
    # dois `period` na mesma coluna x, empilhados → ':'
```

No corpus inteiro isso reduziu a quatro glifos: `U+E088`=`-`, `U+E08C`=`•`,
`U+E092`=`:`, e um sem composto mapeável.

**Regra geral:** antes de rasterizar+OCR (lossy) ou chamar visão (custo e privacidade),
tente o reparo exato lendo as fontes embutidas do próprio PDF. A informação está lá.

## Lição 4 — `OMP_THREAD_LIMIT=1` no tesseract

O OpenMP do tesseract paraleliza mal. Medido no GCF, em 4 cores:

| | sem limite | `OMP_THREAD_LIMIT=1` |
|---|---|---|
| screenshot 540×1170 | 2,2–10,7 s | 2,5–2,9 s |
| página de comprovante | 6,0–13,7 s | 1,5 s |

**4–8× mais rápido e com tempo estável.** Sem isso, um timeout de 20 s dispara em
screenshot comum, e a variância torna qualquer benchmark inútil — foi o que quase me fez
tirar conclusões erradas sobre pré-processamento (ver Lição 5).

Basta `os.environ.setdefault('OMP_THREAD_LIMIT', '1')` antes do pytesseract rodar.

## Lição 5 — Meça acurácia, não a métrica que é fácil de medir

Ao comparar pré-processamentos de imagem, a primeira métrica que usei foi **contagem de
palavras**. Todas as variantes davam 100–105 palavras, e a conclusão teria sido "tanto
faz". Trocando para "quantos campos de referência aparecem no texto", as variantes se
separaram: uma recuperava `0001-26` e `Para`, outra recuperava `Valor`.

Duas consequências práticas:

- **Não existe pré-processamento único que vença sempre.** Em vez de adivinhar, rode duas
  variantes e escolha pela **confiança média do próprio tesseract** (`image_to_data`).
  Custa um OCR a mais e acerta o desempate (conf 89,4 → 5/6 campos contra 83,9 → 4/6).
- **Inverter imagem em modo noturno não melhora acurácia** — o tesseract lê texto claro
  sobre fundo escuro. Melhora só o *tempo* (metade). Vale fazer, mas como normalização,
  não como variante extra.

E um artefato específico de português: o tesseract confunde `a`/`o` com os indicadores
ordinais `ª`/`º` em fontes de app — `Vªlºr`, `pªgªmentº`, `ªpós`. Corrigir quando o
ordinal encosta numa letra (preservando `1ª`, `2º`, `nº`, `Sr.ª`) levou os campos de
referência de 7/10 para 10/10. `-c tessedit_char_blacklist=ªº` também zera os falsos, mas
destrói os legítimos.

---

## Aplicação ao AI-Hub

Nada disso pede mudança de código no AI-Hub hoje. O que pede atenção:

1. Se o AI-Hub passar a aceitar upload de documento para alimentar o ChatGPT, o estágio
   `documento → texto` deve ter portão de qualidade (Lição 1) e reparo de PUA (Lição 3)
   **antes** de decidir mandar a imagem para um modelo de visão. Mandar a imagem é o
   caminho caro e o que mais expõe dado pessoal.
2. Qualquer subprocesso Python do AI-Hub que dependa de libs opcionais deve ter um
   `requirements` próprio e não pode engolir `ImportError` (Lição 2).
3. Se houver OCR em qualquer ponto, `OMP_THREAD_LIMIT=1` (Lição 4).

## Onde está a implementação de referência

- `GestaoContasFernanda/scripts/extract-receipt.py` — cascata, reparo PUA, variantes de OCR
- `GestaoContasFernanda/src/receipt-markdown.ts` — portão `scoreExtraction`, transcrição por LLM
- `GestaoContasFernanda/tests/test_extract_receipt.py` — funções puras
- `GestaoContasFernanda/docs/contratos/markitdown-ocr.md` — contrato completo

## O que continua não medido

Acurácia em **foto de papel**. O corpus real do GCF é 63 PDFs e 2 imagens, e as duas
imagens são screenshots de conversa — não comprovantes. Toda afirmação acima sobre o
caminho de imagem vem de fixtures derivados. Quem tiver um corpus de fotos, meça antes de
confiar.
