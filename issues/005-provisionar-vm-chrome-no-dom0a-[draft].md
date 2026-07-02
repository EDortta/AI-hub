# 005 — Provisionar VM Linux mínima do Chrome no dom0A (browser remoto via CDP)

- work_id: WK-20260701-aihub-vm-chrome
- date: 2026-07-01
- solicitado por: operador
- depende de / decorre de: **004** (desacoplar o browser para VM no dom0)

## Contexto e decisões (correções do operador sobre a 004)

- O alvo de WoL é o **próprio dom0A** (host físico), não uma VM. A VM do Chrome roda
  **dentro** dele.
- **dom0A é KVM/libvirt**, não Xen (`xl`/`xe` inexistentes; `virsh`/`virt-install` presentes).
  → A 004 (que assumia Xen + Windows) fica desatualizada; ver "Impacto na 004".
- **SO da VM: Linux** (não Windows) — ganho vem da VM dedicada + perfil real, não do SO.
- **Energia**: dom0A **desligado fora do horário** (AC só 07:00–18:00, host guloso).
  Fora do horário → **WoL + pagar o cold boot**. Nada de S3 (host físico não suporta;
  `/sys/power/state = disk` apenas). Shutdown em horário acordado (a definir, ~tipo DOM1).
- **Rede**: dom0A (e suas VMs) devem migrar para a **rede 71**; a VM do Chrome ficará
  bridged na LAN 71 para o daemon alcançar o CDP direto (hoje a libvirt usa NAT `default`).

## Medições reais (2026-07-01)

- **Cold boot do dom0A**: WoL → ping/SSH em **~98s** (assinatura de S5/poweroff; ping e
  SSH subiram juntos). Primeiro magic packet não pegou sozinho — precisou reenvio.
- Derivação p/ o polling do `ensure_vm_ready()` no ai-hub:
  - `poll_interval` = 5s · `wol_resend` = 30s · `boot_timeout` = **180s** (98s + folga).
  - `+ CDP/sessão ChatGPT` ainda não medido (Chrome remoto inexistente) → teto total
    provável **~240s**.
- Nível de VM: `virsh managedsave`/`suspend` existe (libvirt) → a VM pode "hibernar pra
  disco" em segundos enquanto o dom0A está ligado, sem custo de RAM parada.

## Inventário do dom0A (2026-07-01)

- RAM 7.6G total, **~4.3G livre** (6 VMs DB rodando) → **RAM é o gargalo**.
- CPU 8 vCPU (folga).
- `vg_dom0` (RAID1): **0 livre**. `vg2_dom0` (sdb): **40.5G livre**. `sdd1`: 97% cheio
  (dados vivos). **Nenhum disco é SSD** (todos HDD 7200rpm).
- Ver também `~/scripts` epic **002-dom0a-manutencao-regular** (guardian de RAM/disco/VMs).

## Spec da VM (mínimo p/ Chrome "a satisfação")

| Item | Decisão |
|---|---|
| Host | dom0A (KVM/libvirt) |
| RAM | **3G** |
| vCPU | 2 |
| Disco | **LV de 16G no pool `vg2_dom0`** (sdd1 e swap-pra-sdd descartados) |
| Guest | Debian 12 minimal, sem desktop |
| Stack | Xvfb + openbox + google-chrome-stable + x11vnc (login/2FA pontual) |
| Chrome | perfil persistente + `--remote-debugging-port=9222`; daemon faz **attach** (não launch) |
| Rede | bridge na LAN 71 (definir junto da migração de rede) |
| Autostart | **desligado** por ora (RAM apertada); ligar sob demanda |

## Plano de build (executar no dom0A quando aprovado)

1. `lvcreate -L 16G -n vm_chrome vg2_dom0` (ou via pool libvirt `vg2_dom0`).
2. `virt-install` Debian 12 (netinst/preseed ou cloud-image + cloud-init), 3G / 2 vCPU,
   disco no LV.
3. Pós-install: Xvfb + openbox + google-chrome-stable + x11vnc; serviço systemd que sobe
   o Chrome com perfil persistente e CDP em `127.0.0.1:9222` (ou LAN 71).
4. VM **sem autostart**; testar CDP local antes de plugar no ai-hub.

## Pontos em aberto (decidir ao retomar)

- **Ordem**: fazer o build agora (NAT) e reconfigurar rede depois, OU fechar a migração
  para rede 71 primeiro e já criar com a bridge certa (evita refazer config). — *pendente*.
- Horário de shutdown/WoL acordado (janela quente).
- Medir CDP + validação de sessão ChatGPT (soma ao `boot_timeout`).
- Segurança do CDP: restringir à LAN interna / túnel; nunca público (ver 004).

## Impacto na 004

A 004 precisa ser reescrita: cai "Windows 11" e toda a tabela WS2012/Win11; cai a premissa
Xen; entra **Linux VM em KVM/libvirt + energia por horário + WoL cold-boot (~98s) + sem S3**.
A 001 (watchdog matando Chrome in-flight) vira **não-aplicável** neste deployment (o Chrome
remoto não é gerenciado pelo watchdog local). Fazer a reescrita da 004 numa próxima sessão.

---

## Nota do agente (2026-07-02) — bloqueada, requer operador

Requer acesso físico/energia ao dom0A, `lvcreate`/`virt-install`, WoL e provisionamento
de VM — fora do que um agente pode/deve executar autonomamente (regra: sem deploy/infra
sem aprovação). Além disso, o guard in-flight adicionado na issue 001 já mitiga o sintoma
de curto prazo (Chrome morto durante geração), reduzindo a urgência desta migração.
Próximo passo é seu: executar o build da VM no dom0A e depois apontar `CDP_URL` do daemon.
