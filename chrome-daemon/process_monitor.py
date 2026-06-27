#!/usr/bin/env python3
"""Monitor e limpa processos playwright/driver/node órfãos do chrome-daemon.

Uso:
    python3 process_monitor.py           # loop contínuo (default)
    python3 process_monitor.py --once    # uma passagem e sai
    python3 process_monitor.py --dry-run # mostra o que faria, sem matar nada
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

try:
    import psutil
except ImportError:
    print("psutil não instalado. Execute: pip install psutil", file=sys.stderr)
    sys.exit(1)

log = logging.getLogger("process-monitor")

# Matar drivers idle com mais de este tempo em segundos.
# Com polling a cada ~1s por watcher, um driver que ficou idle por 5min é genuinamente órfão.
ORPHAN_AGE_SEC = 300

# Verificar a cada N segundos.
CHECK_INTERVAL = 60


def find_daemon_pid() -> int | None:
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = " ".join(p.info["cmdline"] or [])
            if "chrome-daemon/main.py" in cmd:
                return p.pid
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return None


def collect_playwright_drivers(daemon_pid: int) -> list[psutil.Process]:
    """Retorna todos os processos playwright/driver/node filhos do daemon."""
    try:
        daemon = psutil.Process(daemon_pid)
        children = daemon.children(recursive=True)
    except psutil.NoSuchProcess:
        return []

    drivers = []
    for c in children:
        try:
            cmd = " ".join(c.cmdline())
            if "playwright/driver" in cmd and "node" in cmd:
                drivers.append(c)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return drivers


def measure_cpu(processes: list[psutil.Process]) -> dict[int, float]:
    """Mede CPU de uma lista de processos em duas passagens para evitar bloquear."""
    alive = []
    for p in processes:
        try:
            p.cpu_percent(interval=None)  # inicializa sem bloquear
            alive.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    time.sleep(1.0)

    result = {}
    for p in alive:
        try:
            result[p.pid] = p.cpu_percent(interval=None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return result


def cleanup_orphan_drivers(daemon_pid: int, dry_run: bool = False) -> dict:
    drivers = collect_playwright_drivers(daemon_pid)
    now = time.time()

    if not drivers:
        return {"total": 0, "killed": 0, "active": 0}

    cpu_by_pid = measure_cpu(drivers)

    killed = 0
    active = 0

    for p in drivers:
        try:
            pid = p.pid
            cpu = cpu_by_pid.get(pid, 0.0)
            age = now - p.create_time()
            idle = cpu < 0.5 and age > ORPHAN_AGE_SEC

            if not idle:
                active += 1
                log.debug("Mantendo PID %d (cpu=%.1f%%, age=%.0fs)", pid, cpu, age)
                continue

            if dry_run:
                log.info("[DRY-RUN] Terminaria PID %d (cpu=%.1f%%, age=%.0fs)", pid, cpu, age)
            else:
                p.terminate()
                log.info("Terminado driver órfão PID %d (cpu=%.1f%%, age=%.0fs)", pid, cpu, age)
            killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    return {"total": len(drivers), "killed": killed, "active": active}


def run_once(dry_run: bool = False) -> None:
    daemon_pid = find_daemon_pid()
    if daemon_pid is None:
        log.warning("chrome-daemon não encontrado — nada a fazer.")
        return

    result = cleanup_orphan_drivers(daemon_pid, dry_run=dry_run)
    log.info(
        "Daemon PID %d | drivers totais=%d ativos=%d %s=%d",
        daemon_pid,
        result["total"],
        result["active"],
        "seriam-mortos" if dry_run else "mortos",
        result["killed"],
    )


def run_loop(dry_run: bool = False) -> None:
    log.info("Monitor iniciado (intervalo=%ds, orphan_age=%ds).", CHECK_INTERVAL, ORPHAN_AGE_SEC)
    while True:
        try:
            run_once(dry_run=dry_run)
        except Exception:
            log.exception("Erro inesperado no ciclo do monitor.")
        time.sleep(CHECK_INTERVAL)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true", help="Executa uma passagem e sai")
    parser.add_argument("--dry-run", action="store_true", help="Mostra o que faria sem matar nada")
    parser.add_argument("--verbose", "-v", action="store_true", help="Log DEBUG")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.once:
        run_once(dry_run=args.dry_run)
    else:
        run_loop(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
