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
