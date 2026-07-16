# Napkin Lessons — AI-hub

## 2026-07-15 · ensure_xvfb() e o reaper SIGCHLD global

**Sintoma:** ao migrar para um host COM Xvfb instalado (stage4/Debian 12), o daemon
falhava no startup com `Chrome não expôs CDP em http://127.0.0.1:9222 dentro de 60s`,
em loop de restart. No devel3 nunca aconteceu.

**Causa raiz:** `main.py` instala um handler global de `SIGCHLD` (`_reap_children`,
`waitpid(-1, WNOHANG)`) para colher zumbis. Esse handler compete com o
`subprocess.run(["xdpyinfo", ...])` de `ensure_xvfb()`: às vezes o reaper colhe o
xdpyinfo primeiro, e `subprocess.run` retorna `returncode 0` espúrio mesmo quando o
display **não** existe. `ensure_xvfb()` então concluía "display :99 já está de pé",
pulava o start do Xvfb, e o Chrome era lançado contra um display inexistente → CDP
nunca subia.

**Por que só em host com Xvfb:** sem o binário Xvfb, `ensure_xvfb()` retorna `""`
logo no início (fallback headless) e nunca chega a chamar xdpyinfo. O devel3 não
tinha Xvfb até 2026-07-15, então o bug ficou latente.

**Correção (commit 9f01d4d):** exigir também `r.stdout.strip()` — xdpyinfo só imprime
quando o display responde de verdade; o returncode sozinho não é confiável sob o
reaper. Regra geral: qualquer `subprocess.run` cujo status importa é frágil quando
há um handler SIGCHLD global fazendo `waitpid(-1)`.

## 2026-07-15 · sessões web não migram por cópia de perfil

Copiar o `chrome-profile` (ou reinjetar cookies via CDP) NÃO preserva login de
ChatGPT/Google entre hosts: eles invalidam a sessão por mudança de IP/fingerprint.
Planejar sempre re-login manual (x11vnc no Xvfb :99 + túnel SSH) como passo real da
migração, não como contingência.

## [2026-07-16] WK-20260716-ai-issues-sweep — guard no lugar errado, e teste que não existia

**Um guard ao lado do chamador não é um guard.** O guard in-flight da issue 001 foi posto
num ramo do `run_chrome_watchdog`, mas o `SIGKILL` mora em `_kill_stale_chrome()`. Qualquer
outro caminho até o reaper passava por fora. Pior: o reaper poupava só o **PID pai** do
Chrome — e o processo que o log da própria issue mostra sendo morto no meio da geração
(`pid=3568219 age=376s cpu=37.9% display=''`) era um **renderer filho**: velho, quente e
oculto, ou seja, os três critérios de kill ao mesmo tempo. O guard escondia o sintoma; a
proteção não cobria o que dizia cobrir.
*Da próxima vez:* guard vai **dentro** da operação perigosa. E quando a proteção fala de "o
processo", perguntar se ela cobre a **árvore** — renderers são filhos.

**Escopo pela metade sem nota é escopo perdido.** A issue 001 pedia `/image/generate` **ou**
`/conversations/*/send`. Só o primeiro foi feito, e ninguém escreveu que o outro ficou de
fora — então ficou de fora por 14 dias em silêncio.
*Da próxima vez:* implementar o escopo nomeado inteiro, ou escrever qual parte foi pulada e
por quê. Meio guard é um guard com uma exceção que ninguém documentou.

**Afirmar teste que não existe é pior que não testar.** A resolução dizia "lógica do contador
in-flight testada por unit (start/end/underflow/guard)". O repositório não tinha teste
nenhum: nem suíte, nem pytest, nem `tests/`. A frase aposentou o risco na cabeça de todo
leitor seguinte.
*Da próxima vez:* nomear o arquivo e mostrar a execução, ou escrever `não validado: <o quê>`.

**Persistir o registry sem persistir `seen_hashes` troca um bug por outro pior.** Um watcher
restaurado sem o conjunto de hashes vistos relê a conversa inteira e roteia **toda mensagem
antiga para o inbox como se fosse nova** — "o watcher sumiu" viraria "avalanche". O `inbox`,
ao contrário, ficou **fora** do disco de propósito: é texto verbatim do ChatGPT, e gravá-lo
transformaria fila transitória em conteúdo de conversa em repouso.
*Da próxima vez:* ao dar durabilidade a um estado, perguntar de cada campo "o que acontece se
ele **não** voltar?" e "o que acontece se ele **voltar**?" — as duas respostas decidem.
