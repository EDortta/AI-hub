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

## [2026-07-16] WK-20260716-hub-para-ct-4001 — migração para o CT

**Uma afirmação errada num handoff custa mais que um bug.** O handoff dizia que o stage4 era
uma "VM Proxmox Debian 12". Era o **host Proxmox** — o daemon e o Chrome logado rodavam no
hypervisor que serve DHCP, OpenVPN e o `/enviar-arquivo/`. Duas issues (004/005) foram escritas
em cima disso, e eu quase desenhei uma terceira.
*Da próxima vez:* antes de planejar sobre "onde X roda", confirmar no sistema (`/etc/pve`,
`pct list`, `ss -tlnp`). Custa um comando. E ao escrever handoff, "VM" e "host" não são sinônimos.

**Dependência ausente desliga um guard em silêncio.** O CT não tinha `psutil`. O
`_kill_stale_chrome` do watchdog o importa dentro de um `try/except ImportError: return 0` —
sem psutil, o watchdog inteiro vira no-op e **não avisa**. O guard existia no código e não
existia na máquina.
*Da próxima vez:* guard que degrada para no-op quando falta dependência precisa **logar** a
degradação. E validar dependência no boot, não no primeiro uso.

**`pgrep` no host de LXC conta os processos do container.** `pgrep -c chrome` no hypervisor
retornou 22 depois da migração — o LXC compartilha o `/proc`. Só o `cgroup`
(`/lxc/4001/...`) e o pid namespace distinguem. Quase concluí que tinha falhado em limpar o host.
*Da próxima vez:* para provar "o host está limpo", use `grep lxc /proc/<pid>/cgroup`, nunca `pgrep`.

**Copiar o código do host em vez do repo.** Empacotei o `chrome-daemon` de `/home/ai-hub/...`
(a versão implantada) e mandei só o `main.py` novo por cima → `ImportError` em loop de restart.
*Da próxima vez:* deploy sai do repo, com tudo junto; nunca misture um arquivo novo com uma
árvore velha.

**A sessão não sobrevive nem para container na mesma caixa.** Perfil de 3.4G copiado,
`logged_in: false`. A lição de 2026-07-15 (perfil não migra entre hosts) vale também aqui.
*Da próxima vez:* planeje o re-login manual como passo do plano, não como contingência.

**x11vnc precisa rodar como o dono do Xvfb, e com `-noshm`.** A receita que passei ao operador
falhou com `X Error: BadAccess ... X_ShmAttach`: o `ssh ai-ecosystem` entra como **root**, mas o
Xvfb roda como **ai-hub** — um processo não anexa a memória compartilhada do X server de outro
usuário. Escrevi a receita sem ligar que a própria entrada de ssh que eu criei entra como root.
*Da próxima vez:* `su - ai-hub -c "x11vnc ... -noshm"`. E ao escrever receita que envolve X,
conferir **quem é dono do display** (`ps -o user= -C Xvfb`), não presumir.

**Apagar o home de um serviço migrado pode ressuscitar um bug antigo.** Ia remover
`/home/ai-hub` do host depois da migração — mas o **guardian roda no host e lê o token de
`/home/ai-hub/.config/ai-hub/daemon.env`**. Sem token → curl 401 → guardian conclui "caiu" →
**reinicia um daemon são**: exatamente o `DIAG-20260707`. Só o perfil (3.4G) e o binário do
Chrome saíram.
*Da próxima vez:* antes de apagar o home de um serviço migrado, `grep -rl "/home/<user>"` em
`/etc/cron.d`, `/usr/local/sbin` e units — o que sobrou pode ser dependência de outra coisa.

**Código certo falhando é sinal de estado, não de lógica — reproduza a sequência EXATA.** O
delete do chat (003) falhava com seletores que eu tinha verificado à mão funcionando. A
diferença: meu teste dava `Escape` antes; o daemon não, e o primeiro clique fechava o overlay
deixado pela geração em vez de abrir o menu. Perdi dois testes achando que era seletor errado.
*Da próxima vez:* quando o código parece certo e falha, replique a sequência do processo real
(mesma ordem, mesmo estado inicial), não um trecho isolado num contexto limpo. O contexto
limpo é justamente o que esconde o bug.

**Um seletor que casa 38 elementos com `.last` é uma bomba, não um fallback.** O delete caía
em `aria-label*='options'`, que casava os botões de opção de cada conversa da sidebar. Com
`.last`, teria apagado outra conversa do operador se o item Delete fosse encontrado ali.
*Da próxima vez:* ao escrever seletor de UI para uma AÇÃO DESTRUTIVA, contar quantos elementos
ele casa (`.count()`) na UI real — não confiar que "provavelmente é o certo". Um seletor
destrutivo ambíguo é pior que nenhum.
