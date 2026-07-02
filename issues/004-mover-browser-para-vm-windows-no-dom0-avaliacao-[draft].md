# 004 — Desacoplar o browser: rodar Chrome/ChatGPT numa VM Windows (dom0)

- work_id: WK-20260701-aihub-browser-vm-windows
- date: 2026-07-01
- solicitado por: operador

## Motivação

A fragilidade da geração (issues 001/002) é sintoma de um problema de raiz: o **"Chrome
oculto"** — Chrome sob Xvfb (`display=:99`) no mesmo host, dirigido por Playwright/CDP,
concorrendo com o watchdog e sujeito a detecção de automação. O operador prefere rodar o
browser numa **VM Windows no dom0** (Xen), *sem sequer ver a execução*, com uma sessão
ChatGPT real e persistente. O daemon conectaria ao Chrome remoto via **CDP pela rede
interna**.

## Avaliação — é solução real? **Sim, com uma ressalva de versão do Windows.**

Desacoplar o browser para um desktop Windows dedicado e sempre-ligado é legitimamente mais
robusto que Chrome headless no host: sessão logada persistente num perfil real, menos
detecção de bot, e o watchdog do host deixa de matar o browser. Atende também a preferência
de "execução invisível" no dom0.

### Candidatos

| VM | Veredito |
|---|---|
| **Windows Server 2012 (licenciada)** | ❌ **Não recomendado.** EOL (suporte estendido acabou em 2023-10-10) e **o Chrome moderno não suporta Server 2012/R2** (Chrome 110+ exige Win10/Server 2016+). Uma sessão ChatGPT exige browser atualizado → inviável, apesar da licença. |
| **Windows 11 (VM local)** | ✅ **Recomendado.** Roda Chrome atual; melhor compatibilidade. Hospedar no dom0 conforme a preferência. |
| Windows Server 2019/2022 (se houver/licenciável) | ✅ Ideal "server-grade e invisível" no dom0, se disponível. |

### Arquitetura proposta

- Chrome na VM com `--remote-debugging-port=9222 --remote-debugging-address=127.0.0.1` +
  **perfil persistente** logado no ChatGPT.
- Expor o CDP **só na rede interna** (túnel SSH / rede privada Xen) — **nunca** público.
- `chrome-daemon` conecta via `CDP_URL` apontando para a VM; watchdog local passa a não
  gerenciar/matar o Chrome remoto (respeitar browser remoto — ver 001).
- Latência de CDP pela rede interna é irrelevante para este fluxo.

### Riscos / pontos a validar

- Manter o perfil logado (2FA/cookies) estável na VM.
- Segurança: CDP dá controle total do browser — restringir a `127.0.0.1` da VM + túnel.
- Custo de manter a VM sempre ligada no dom0.

## Comportamento esperado

- `CDP_URL` aponta para a VM Windows; geração roda no Chrome real da VM; host não mata mais
  o browser; execução invisível no dom0.

## Impacto

- Positivo: robustez de raiz para 001/002; sessão ChatGPT estável; alinha à preferência do
  operador.
- Regressão: mudança de deployment; exige config de rede/CDP e um perfil Windows dedicado.

> **Decisão do operador (2026-07-01): Windows 11.** WS2012 descartada (EOL + Chrome
> incompatível). Próximo passo: preparar a VM Win11 no dom0 com Chrome (perfil persistente
> logado no ChatGPT) e CDP na rede interna; depois apontar `CDP_URL` do daemon para a VM.

---

## Nota do agente (2026-07-02) — bloqueada, requer operador

Issue de arquitetura/infra, **não resolvível autonomamente**. Superada pela 005
(Linux VM em KVM/libvirt no dom0A, não Windows/Xen). Recomendação: reescrever esta
issue apontando para a 005 ou fechá-la como `[superseded]`. Decisão do operador.
