#!/usr/bin/env bash
# aihub-vnc.sh — abre a tela do Chrome do AI-hub (CT 4001 do stage4) para mexer na mão.
#
# Para quê: re-login no ChatGPT (2FA), trocar o modelo do GPT, aceitar um diálogo —
# tudo que exige olho e mão humana no browser que o daemon dirige.
#
# O Chrome roda sob Xvfb :99 dentro do CT, sem tela. Este script sobe um x11vnc
# **temporário** lá, abre um túnel SSH até ele, chama o visualizador, e **derruba
# tudo ao sair**. O x11vnc é `-nopw`: quem alcança a porta dirige o browser logado.
# Por isso ele nasce e morre com este script — nunca fica de pé.
#
# Uso:
#   ./aihub-vnc.sh            # abre a tela (bloqueia; feche o visualizador ou Ctrl-C)
#   ./aihub-vnc.sh --check    # diz se dá para abrir, e sai
#   ./aihub-vnc.sh --kill     # mata x11vnc órfão no CT (se algo ficou pendurado)
#
# Pegadinhas que este script existe para você não repetir (todas custaram caro):
#   - x11vnc PRECISA rodar como `ai-hub`, o dono do Xvfb. Rodar como root (que é
#     como `ssh ai-ecosystem` entra) falha com `X Error: BadAccess ... X_ShmAttach`.
#   - PRECISA de `-noshm`: o MIT-SHM não funciona neste ambiente.
#   - `-localhost`: o VNC nunca escuta fora do loopback do CT; só se chega por túnel.
set -u

REMOTE="ai-ecosystem"        # ~/.ssh/config → ProxyJump stage4-inovacao → CT 4001
VNC_USER="ai-hub"            # dono do Xvfb :99 — NÃO troque para root
DISPLAY_NUM=":99"
REMOTE_PORT=5900
LOCAL_PORT="${AIHUB_VNC_LOCAL_PORT:-5900}"

CTRL_SOCK="/tmp/.aihub-vnc-ctrl.$$"

log() { echo "[aihub-vnc] $*"; }
die() { echo "[aihub-vnc] $1" >&2; exit "${2:-1}"; }

remote_kill_vnc() { ssh -o BatchMode=yes "$REMOTE" 'pkill x11vnc 2>/dev/null; true' >/dev/null 2>&1; }

cleanup() {
    log "encerrando…"
    # Encerra o túnel pelo socket de controle, não por pgrep: pgrep casa a
    # própria linha de comando de quem procura e mente na cara do operador.
    [[ -S "$CTRL_SOCK" ]] && ssh -S "$CTRL_SOCK" -O exit "$REMOTE" 2>/dev/null
    remote_kill_vnc
    log "x11vnc derrubado e túnel fechado."
}

case "${1:-}" in
  --kill)
    remote_kill_vnc
    log "x11vnc morto no CT (se havia)."; exit 0 ;;
  --check)
    ssh -o BatchMode=yes -o ConnectTimeout=8 "$REMOTE" true 2>/dev/null \
        || die "não alcanço '$REMOTE' por ssh (ProxyJump pelo stage4)." 2
    ssh -o BatchMode=yes "$REMOTE" "su - $VNC_USER -c 'DISPLAY=$DISPLAY_NUM xdpyinfo' >/dev/null 2>&1" \
        || die "o display $DISPLAY_NUM não responde no CT — o Xvfb está de pé? (systemctl status xvfb)" 3
    command -v gvncviewer >/dev/null || command -v remmina >/dev/null \
        || die "nenhum visualizador VNC aqui (instale gvncviewer ou remmina)." 4
    log "tudo pronto: ssh OK, display $DISPLAY_NUM OK, visualizador OK."; exit 0 ;;
esac

# --- pré-checagens (falhar aqui é melhor que falhar com a tela na cara) -------
ssh -o BatchMode=yes -o ConnectTimeout=8 "$REMOTE" true 2>/dev/null \
    || die "não alcanço '$REMOTE' por ssh. Confira a entrada no ~/.ssh/config." 2

ssh -o BatchMode=yes "$REMOTE" "su - $VNC_USER -c 'DISPLAY=$DISPLAY_NUM xdpyinfo' >/dev/null 2>&1" \
    || die "display $DISPLAY_NUM não responde no CT — Xvfb caiu? (ssh $REMOTE systemctl status xvfb)" 3

VIEWER=""
command -v gvncviewer >/dev/null && VIEWER="gvncviewer"
[[ -z "$VIEWER" ]] && command -v remmina >/dev/null && VIEWER="remmina"
[[ -z "$VIEWER" ]] && die "nenhum visualizador VNC (instale gvncviewer ou remmina)." 4

if ss -ltn 2>/dev/null | grep -q "127.0.0.1:${LOCAL_PORT} "; then
    die "porta local ${LOCAL_PORT} ocupada. Feche o que está lá, ou:
     AIHUB_VNC_LOCAL_PORT=5901 $0" 5
fi

trap cleanup EXIT INT TERM

# --- x11vnc temporário no CT --------------------------------------------------
# Mata sobras antes: dois x11vnc no mesmo display brigam pela porta.
remote_kill_vnc
log "subindo x11vnc no CT (como $VNC_USER, -noshm, só loopback)…"
ssh -o BatchMode=yes "$REMOTE" \
    "su - $VNC_USER -c 'nohup x11vnc -display $DISPLAY_NUM -localhost -nopw -noshm -forever -quiet >/tmp/x11vnc.log 2>&1 &'" \
    || die "não consegui subir o x11vnc no CT." 6
sleep 2

ssh -o BatchMode=yes "$REMOTE" "ss -tln | grep -q '127.0.0.1:${REMOTE_PORT}'" \
    || die "x11vnc não escutou em ${REMOTE_PORT}. Log: ssh $REMOTE cat /tmp/x11vnc.log" 6

# --- túnel --------------------------------------------------------------------
# `-f` (e não `... &`): o -f só desacopla DEPOIS de autenticar e montar o
# forward. Com `&`, o shell segue na hora e o túnel ainda nem autenticou — o
# caminho aqui tem dois saltos (frida-hub → stage4 → CT) e leva alguns segundos.
# `-M -S` dá um socket de controle para encerrar com precisão no cleanup.
log "abrindo túnel devel3:${LOCAL_PORT} → CT:${REMOTE_PORT}…"
ssh -f -N -M -S "$CTRL_SOCK" -o BatchMode=yes -o ExitOnForwardFailure=yes \
    -L "${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" "$REMOTE" \
    || die "o túnel não subiu (ssh -f falhou)." 7

# Handshake RFB: prova que o VNC responde de verdade antes de abrir a janela.
# Com retry — o -f garante o forward montado, mas o primeiro pacote ainda
# atravessa dois saltos.
rfb=""
for _ in 1 2 3 4 5; do
    rfb=$(timeout 4 bash -c "exec 3<>/dev/tcp/127.0.0.1/${LOCAL_PORT}; head -c 12 <&3" 2>/dev/null | tr -d '\0')
    [[ "$rfb" == RFB* ]] && break
    sleep 1
done
[[ "$rfb" == RFB* ]] || die "o túnel subiu mas o VNC não respondeu (sem handshake RFB)." 7
log "handshake: $rfb"

log "conectado. Abrindo $VIEWER — feche a janela (ou Ctrl-C) para encerrar tudo."
log ""
log "  Lembretes do que costuma ser feito aqui:"
log "   • re-login no ChatGPT (2FA)  → depois: /session/check deve dar logged_in:true"
log "   • modelo do GPT preso em 'Thinking' → trocar para o padrão (issue 009)"
log ""

if [[ "$VIEWER" == "gvncviewer" ]]; then
    gvncviewer "127.0.0.1:0"          # gvnc: display 0 = porta 5900
else
    remmina -c "vnc://127.0.0.1:${LOCAL_PORT}"
fi
