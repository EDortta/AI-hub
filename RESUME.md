# RESUME — AI-Hub

work_id: WK-20260518-ai-hub-playwright-fix
date: 2026-05-18

## Estado atual

Daemon rodando, Chrome headless (sem Xvfb). Serviços:

```
chrome-daemon.service   active  PID ~3427412
gcf-clara.service       active
gcf-bridge.service      active  (ver alerta abaixo)
```

Último commit: `148fcd0` — `ai-hub send` / `ai-hub read` na CLI.

---

## Next Step (DO THIS FIRST)

**Verificar por que o watcher Claudia não está registrado.**

```bash
curl http://127.0.0.1:9400/conversations
```

Resultado esperado: lista com alias `Claudia`. Resultado atual: `[]`.

O gcf-bridge estava falhando com "AI-Hub daemon não encontrado" (timing no boot —
bridge subia antes do daemon estar pronto). Após reinicializações, ambos estão `active`,
mas o watcher pode não ter sido registrado se a última tentativa da bridge ainda falhou.

Diagnóstico:

```bash
journalctl --user -u gcf-bridge -n 20 --no-pager
# procurar "Registrado" ou erro
```

Se watcher não estiver lá:

```bash
systemctl --user restart gcf-bridge
sleep 3
curl http://127.0.0.1:9400/conversations
```

---

## O que foi feito nesta sessão

### Problema central resolvido
"Playwright Sync API inside asyncio loop" — todos os `run_in_executor(None, ...)` que
tocam Playwright foram migrados para `playwright_executor` (ThreadPoolExecutor com
`initializer=lambda: asyncio.set_event_loop(None)`) definido em `chrome_manager.py`.

Arquivos alterados: `chrome_manager.py`, `main.py`, `watchers.py`.

### Outros fixes
- `chrome_manager.py`: `ensure_xvfb()` retorna `""` quando Xvfb não instalado →
  `launch_chrome()` usa `--headless=new` em vez de falhar.
- `chrome-daemon.service`: removido `After=graphical-session.target` que impedia boot
  sem sessão gráfica.
- `_attach_reference_image` fallback: re-clica o botão de anexo dentro do
  `expect_file_chooser` context (antes fazia `pass`, causando timeout).

### Novos recursos
- `POST /social/publish/x` e `POST /social/publish/linkedin` (main.py)
- `social_publisher.py` — extraído do daily_post.py do Dopamin Captain
- `client.py`: `publish_to_x()`, `publish_to_linkedin()`, `reference_image_path` em
  `generate_image()`
- `cli.py`: `ai-hub send <alias> <texto>` e `ai-hub read <alias>`
- `GestaoContasFernanda/AGENTS.md`: seção do canal Sofia/Claudia com comandos de uso
- `GestaoContasFernanda/ORIENTACAO.md`: arquitetura, diagnóstico, o que não fazer
- `README.md`: atualizado com todos os endpoints, nota do playwright_executor, Xvfb vs
  headless

### Dopamin Captain (`daily_post.py`)
Migrado de 1543 para 802 linhas: removido todo Chrome/Playwright direto, agora usa
`AIHubClient.generate_image()` + `publish_to_x()` + `publish_to_linkedin()`.

---

## Pendências conhecidas

| Item | Estado |
|------|--------|
| `gcf-bridge` registrando watcher Claudia consistentemente | A verificar |
| Testar `weekly_comic.py --generate-image` (Yeam & Yeamima) | Não testado após fix do playwright_executor |
| `watchers.py` importa `AsyncChromeManager` (mudança externa) | Não verificado se `AsyncChromeManager` existe em `chrome_manager.py` |

---

## Arquitetura de serviços

```
chrome-daemon.service  (porta 9400)
    └── Chrome headless --headless=new (porta CDP 9222)
        └── ~/.local/share/ai-hub/chrome-profile/

gcf-clara.service      (Node.js, WhatsApp via wweb.js)
gcf-bridge.service     (porta 9401, Python)
    └── registra watcher "Claudia" no daemon
    └── recebe callbacks em POST /message
    └── roteia para Clara

GestaoContasFernanda/.ai-hub.yml
    url: https://chatgpt.com/c/6a07fa5a-3254-83e9-a5f3-ecd4ed6035e8
    alias: Claudia  ←→  chatgpt_alias: Sofia
    callback: http://localhost:9401/message
```

## Convenção de mensagens Sofia ↔ Claudia

- Agente → Sofia: `"Hey, Sofia, <texto>"`  via `ai-hub send Claudia "Hey, Sofia, ..."`
- Sofia → Claudia: `"Hey, Claudia, <texto>"` detectado pelo daemon → callback bridge
