#!/usr/bin/env bash
# chrome-login.sh — abre o Chrome do AI-Hub visível para login manual.
#
# Fluxo:
#   1. Para o chrome-daemon (se ativo) — impede o watchdog de relançar o
#      Chrome escondido enquanto você usa a janela visível.
#   2. Fecha o Chrome escondido de forma limpa (SIGTERM → espera → SIGKILL).
#   3. Abre o Chrome VISÍVEL no seu display, com o MESMO perfil
#      (~/.local/share/ai-hub/chrome-profile) e o mesmo CDP na porta 9222.
#      Faça login nos serviços que quiser.
#   4. Quando você fechar a última janela do Chrome (ou Ctrl-C aqui),
#      tudo volta ao lugar: o daemon é religado (se estava ativo) ou o
#      Chrome escondido é relançado no Xvfb :99 — os cookies/sessões que
#      você criou ficam disponíveis para o AI-Hub e subsidiários.
#
# Uso:
#   ./chrome-login.sh [URL inicial opcional]

set -u

# --- Configuração (espelha chrome_manager.py) -------------------------------
PROFILE_DIR="$HOME/.local/share/ai-hub/chrome-profile"
CDP_PORT=9222
CDP_URL="http://127.0.0.1:${CDP_PORT}"
XVFB_DISPLAY=":99"
SERVICE="chrome-daemon.service"
VISIBLE_DISPLAY="${DISPLAY:-:0}"
START_URL="${1:-}"

CHROME_BIN="$(command -v google-chrome || command -v google-chrome-stable || true)"
if [[ -z "$CHROME_BIN" ]]; then
    echo "ERRO: google-chrome não encontrado no PATH." >&2
    exit 1
fi

log() { printf '[chrome-login] %s\n' "$*"; }

# --- Helpers -----------------------------------------------------------------

cdp_available() {
    curl -sf -m 2 "${CDP_URL}/json/version" >/dev/null 2>&1
}

singleton_pid() {
    # SingletonLock é um symlink "host-PID"; devolve o PID se o processo vive.
    local target pid
    target="$(readlink "$PROFILE_DIR/SingletonLock" 2>/dev/null)" || return 1
    pid="${target##*-}"
    [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null && echo "$pid"
}

remove_singleton_files() {
    rm -f "$PROFILE_DIR/SingletonCookie" \
          "$PROFILE_DIR/SingletonLock" \
          "$PROFILE_DIR/SingletonSocket" 2>/dev/null
}

kill_profile_chrome() {
    # Fecha (limpo) qualquer Chrome usando o perfil do AI-Hub.
    local pid
    pid="$(singleton_pid || true)"
    if [[ -z "$pid" ]]; then
        # Fallback: processo principal com este user-data-dir (sem --type=).
        pid="$(pgrep -f -- "--user-data-dir=${PROFILE_DIR}" | head -1 || true)"
    fi
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        log "Fechando Chrome (PID $pid) com SIGTERM..."
        kill -TERM "$pid" 2>/dev/null
        for _ in $(seq 1 20); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.5
        done
        if kill -0 "$pid" 2>/dev/null; then
            log "Não fechou em 10s — enviando SIGKILL."
            kill -KILL "$pid" 2>/dev/null
            sleep 1
        fi
    fi
    # Restos (renderers órfãos) do mesmo perfil.
    pkill -f -- "--user-data-dir=${PROFILE_DIR}" 2>/dev/null
    sleep 0.5
    remove_singleton_files
}

# Flags base — mesmas do chrome_manager.py (SEC-0036/SEC-0107 preservadas).
chrome_base_args() {
    echo "--user-data-dir=${PROFILE_DIR}" \
         "--profile-directory=Default" \
         "--no-first-run" \
         "--disable-dev-shm-usage" \
         "--remote-debugging-port=${CDP_PORT}" \
         "--remote-allow-origins=" \
         "--disable-blink-features=AutomationControlled" \
         "--exclude-switches=enable-automation" \
         "--disable-automation" \
         "--disable-features=OptimizationGuideOnDeviceModel,OptimizationGuideModelDownloading,OptimizationHints,TextSafetyClassifier,OnDeviceHeadSuggest" \
         "--disable-component-update"
}

ensure_xvfb() {
    command -v Xvfb >/dev/null || { echo ""; return; }
    if xdpyinfo -display "$XVFB_DISPLAY" >/dev/null 2>&1; then
        echo "$XVFB_DISPLAY"; return
    fi
    setsid Xvfb "$XVFB_DISPLAY" -screen 0 1280x1024x24 >/dev/null 2>&1 &
    sleep 1.5
    echo "$XVFB_DISPLAY"
}

wait_cdp() {
    for _ in $(seq 1 30); do
        cdp_available && return 0
        sleep 1
    done
    return 1
}

# --- Restauração (trap garante que roda mesmo com Ctrl-C) --------------------

DAEMON_WAS_ACTIVE=0
RESTORED=0

restore() {
    [[ "$RESTORED" == 1 ]] && return
    RESTORED=1
    echo
    log "Janela fechada — devolvendo tudo ao lugar..."

    # Garante que o Chrome visível terminou de verdade antes de relançar.
    kill_profile_chrome

    if [[ "$DAEMON_WAS_ACTIVE" == 1 ]]; then
        log "Religando ${SERVICE} (ele mesmo relança o Chrome escondido)..."
        systemctl --user start "$SERVICE"
    else
        local disp
        disp="$(ensure_xvfb)"
        if [[ -n "$disp" ]]; then
            log "Relançando Chrome escondido no Xvfb ${disp}..."
            DISPLAY="$disp" setsid "$CHROME_BIN" $(chrome_base_args) \
                --disable-gpu --window-size=1280,1024 --new-window \
                >/dev/null 2>&1 &
        else
            log "Xvfb indisponível — relançando Chrome em headless nativo..."
            env -u DISPLAY setsid "$CHROME_BIN" $(chrome_base_args) \
                --disable-gpu --window-size=1280,1024 --headless=new \
                >/dev/null 2>&1 &
        fi
    fi

    if wait_cdp; then
        log "OK: Chrome escondido de volta — CDP disponível em ${CDP_URL}."
        log "Sessões/logins feitos na janela visível estão salvos no perfil."
    else
        log "AVISO: CDP não respondeu em 30s. Verifique com: curl ${CDP_URL}/json/version"
    fi
}
trap restore EXIT INT TERM

# --- 1. Impedir relaunch em background ---------------------------------------

if systemctl --user is-active --quiet "$SERVICE"; then
    DAEMON_WAS_ACTIVE=1
    log "Parando ${SERVICE} (impede o watchdog de reabrir o Chrome em background)..."
    systemctl --user stop "$SERVICE"
fi

# Daemon iniciado à mão (fora do systemd) também relançaria o Chrome — encerra.
MANUAL_DAEMON_PID="$(pgrep -f 'chrome-daemon/main.py' || true)"
if [[ -n "$MANUAL_DAEMON_PID" ]]; then
    log "Daemon manual detectado (PID $MANUAL_DAEMON_PID) — encerrando com SIGTERM."
    log "AVISO: ele NÃO será religado automaticamente ao final (foi iniciado fora do systemd)."
    kill -TERM $MANUAL_DAEMON_PID 2>/dev/null
    sleep 2
fi

# --- 2. Fechar o Chrome escondido ---------------------------------------------

kill_profile_chrome
log "Chrome escondido fechado."

# --- 3. Abrir Chrome visível ---------------------------------------------------

log "Abrindo Chrome VISÍVEL em DISPLAY=${VISIBLE_DISPLAY} com o perfil do AI-Hub."
log "Faça seus logins. Ao fechar a última janela, tudo volta ao normal."

# Foreground: o script fica bloqueado aqui até você fechar a última janela.
DISPLAY="$VISIBLE_DISPLAY" "$CHROME_BIN" $(chrome_base_args) \
    --window-size=1500,1000 --window-position=100,100 --new-window \
    ${START_URL:+"$START_URL"} \
    >/dev/null 2>&1

# --- 4. restore() roda via trap EXIT -------------------------------------------
exit 0
