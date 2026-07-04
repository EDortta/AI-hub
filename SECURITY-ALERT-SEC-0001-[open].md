# 🔴 ALERTA DE SEGURANÇA — SEC-0001 (ação do operador/interna) — [open]

**Status:** aberto · **Severidade:** crítica · **Branch da correção:** WK-20260704-security-fixes

## Já feito (código)
Auth por token no daemon (fail-closed 503 sem token), guard de Host, cliente atualizado.

## Pendente — AÇÃO SUA/INTERNA
- [ ] Definir `AIHUB_DAEMON_TOKEN` (`openssl rand -hex 32`) no ambiente do **daemon e dos clientes**
      via `EnvironmentFile=` no unit systemd (fora do repo). Sem isso o daemon fica em 503.
- [ ] Revisar/mergear a branch e reiniciar o serviço manualmente (deploy autônomo proibido).
