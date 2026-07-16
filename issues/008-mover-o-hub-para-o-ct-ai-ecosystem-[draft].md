# 008 — Mover o hub para o CT `ai-ecosystem` (4001), tirando Chrome e CDP do hypervisor

- work_id: `WK-20260716-hub-para-ct-4001`
- date: 2026-07-16
- tipo: **infra / segurança**
- status: draft
- solicitado por: operador (Esteban), 2026-07-16
- decisões do operador: **`pct`** (não Incus); **o Gateway fica no devel3**
- relacionado: `004`/`005` `[superseded]` (queriam browser em VM dedicada);
  `AI-Agents/docs/issues/004-deploy-gateway-hub-proxmox-[superseded-partial]`

## Motivação — onde o hub está hoje

Recon read-only do stage4 (2026-07-16). O daemon, o Chrome e o **perfil logado do ChatGPT**
rodam **direto no hypervisor Proxmox**:

```
ps:  402890 ai-hub Xvfb  ·  402912 ai-hub chrome  ·  python3 402887
ss:  127.0.0.1:9222  chrome        ← CDP
     127.0.0.1:9400  python3       ← daemon
     0.0.0.0:9480    nginx (host)  ← vhost ai-hub.conf
/etc/pve presente → stage4 É o host Proxmox, não uma VM.
/etc/nginx/sites-enabled/: ai-hub.conf, steward.conf, zeecred-sftp
```

Três problemas, em ordem de gravidade:

1. **O CDP em `127.0.0.1:9222` é do hypervisor.** Qualquer processo ou usuário local do
   stage4 pode anexar e **dirigir o browser logado** — sem token, sem auth. O
   `CDP-ACCESS.md` já documenta que "não dá pra ter dois donos do mesmo browser-endpoint",
   mas o modelo de ameaça real é outro: o CDP não tem autenticação nenhuma, e o
   `--remote-allow-origins=` (SEC-0036) só barra páginas de browser, **não** outro processo
   local. A joia da coroa — a sessão ChatGPT autenticada, que só se recria com 2FA na mão —
   está exposta a toda a caixa.
2. **A caixa é a errada.** stage4 roda a infra que a epic 004 declarou intocável: DHCP
   (`1001 dnsmasq`), nginx do `:80`, `/enviar-arquivo/`, túnel reverso, OpenVPN. Um exploit
   de renderer do Chrome cai **ali**.
3. **O hub já escreveu no nginx do host.** `ai-hub.conf` está ao lado de `steward.conf` e
   `zeecred-sftp`. Foi aditivo (vhost novo, porta nova) e não quebrou nada — mas contraria a
   letra do desenho ("nginx **dentro** do container").

## O achado que muda o custo desta issue

**O CT alvo já existe, e está configurado certo.** `pct config 4001`:

```
hostname: ai-ecosystem     unprivileged: 1     features: nesting=1
memory: 3072   cores: 2    rootfs: local-lvm:vm-4001-disk-0, 12G (8.9G livres)
net0: name=eth0, bridge=vmbr0, ip=192.168.7.201/24, gw=192.168.7.6
```

- `unprivileged: 1` + **`features: nesting=1`** → é exatamente o que o sandbox de
  user-namespace do Chrome precisa. **Não vamos ter que desligar o sandbox** (SEC-0107).
- `3072M / 2 cores` → a spec que a issue 005 derivou para Chrome "a satisfação".
- Já instalados dentro: **Xvfb**, **python3**, nginx, Postgres, postfix. RAM: 2.8G de 3G
  livres (ocioso).
- **Faltam**: `google-chrome-stable` e o próprio hub.

Ou seja: alguém provisionou o `ai-ecosystem` seguindo o runbook do epic 004, instalou as
dependências, e a migração de 2026-07-15 instalou o hub **no host** mesmo assim. Esta issue
**termina** essa migração; não começa uma.

## Escopo

1. **Instalar no CT 4001**: `google-chrome-stable`, dependências do `requirements.txt`,
   o `chrome-daemon` (via `install/install.sh`, que já deriva paths de `$DAEMON_DIR`),
   usuário dedicado `ai-hub` + `loginctl enable-linger`, systemd user service.
2. **Validar o sandbox**: Chrome sobe **sem** `--no-sandbox` dentro do CT unprivileged.
   *Se não subir, PARE* — ver "Linha vermelha".
3. **Re-login no ChatGPT** via `x11vnc -display :99 -localhost` + túnel SSH. **Passo humano,
   com 2FA.** O perfil **não** migra (napkin do repo: ChatGPT/Google invalidam a sessão por
   mudança de IP/fingerprint).
4. **Repontar o nginx do host**: `ai-hub.conf` `proxy_pass http://127.0.0.1:9400` →
   o CT. Ver "Decisão A".
5. **Limpar o host**: parar/desabilitar o service do `ai-hub`, remover o usuário e o
   `chrome-profile` do hypervisor. **Só depois do CT provado** — o perfil do host é o
   rollback até lá.
6. **Guardian**: `/usr/local/sbin/ai-hub-guardian.sh` + `/etc/cron.d/ai-hub` passam a
   checar o daemon **no CT** (`pct exec 4001 -- systemctl --user -M ai-hub@ ...` ou o
   health via nginx). Hoje reiniciam o do host.

## Decisão A — como o nginx do host alcança o daemon (precisa ser tomada)

O daemon hoje faz bind em `127.0.0.1:9400`. Dentro do CT, **o loopback do CT não é o do
host**: o nginx do host não alcança `127.0.0.1:9400` do container. Três saídas:

| Opção | Como | Custo / risco |
|---|---|---|
| **A1** — daemon bind em `0.0.0.0:9400` no CT; host nginx → `192.168.7.201:9400` | 1 hop | O `:9400` fica **alcançável da LAN inteira** (o CT tem IP em vmbr0). Depende só de token + Host-allowlist para se defender. Contraria "só expomos o que precisamos". |
| **A2** — daemon segue em `127.0.0.1:9400`; **nginx do CT** (já instalado) escuta na eth0 e faz proxy p/ loopback; host nginx → `192.168.7.201:80` | 2 hops | Daemon nunca escuta fora do loopback do CT. É a forma do bundle `004-04` (`proxy_set_header Host localhost` resolve a allowlist). Um nginx a mais para manter. |
| **A3** — A1 + **firewall do Proxmox** no CT, liberando `:9400` só para o IP do host | 1 hop | Mais simples que A2, mas a proteção mora fora do CT (regra de firewall), não no bind. |

**Recomendação: A2.** O daemon não escutar fora do loopback é uma propriedade do serviço,
não uma regra que alguém pode apagar depois — e o nginx do CT já está de pé. Bônus: o bloco
`/api-hub/` do `004-04` (que eu havia marcado como premissa caduca) volta a ser exatamente
o artefato certo, só trocando o upstream.

## Decisão B — o CT tem IP na LAN de qualquer forma

`192.168.7.201/24` em `vmbr0`. "Fechado lá dentro" **não é automático**: o CT é alcançável
da rede. O ganho desta issue é preciso e vale por si — **o CDP e o `chrome-profile` saem do
alcance de todo processo do hypervisor** —, mas não confunda com isolamento de rede. Se o
objetivo incluir fechar o CT para a LAN, é regra de firewall do Proxmox, escopo separado.

## Linha vermelha (não negociável)

**Se o Chrome não subir sem `--no-sandbox` dentro do CT, pare e reavalie — não desligue o
sandbox.** O container protege o *host*; o sandbox protege o *perfil logado* de um renderer
comprometido. Trocar um pelo outro não é ganho, é troca lateral — e o perfil é o ativo mais
caro (2FA manual para recriar). `nesting=1` já está ligado justamente para isto.
Ver `security-standards.md` §3: "flags que desligam proteções nunca são default".

## Comportamento esperado

- `AiHubDriver.health()` **do devel3** → `('UP', None)` contra `http://192.168.7.200:9480`,
  igual a hoje — **o Gateway não muda de config** (decisão do operador: Gateway local, hub
  remoto; é justamente essa conexão que se quer exercitar).
- `ps -eo user,comm | grep -E "chrome|Xvfb"` **no host** → vazio.
- `ss -tlnp | grep 9222` **no host** → vazio.
- Geração de imagem e `/conversations/*/send` funcionando de dentro do CT.

## Impacto

- **Positivo**: CDP e sessão ChatGPT saem do hypervisor; renderer do Chrome fica contido;
  o hypervisor volta a ser só hypervisor + infra.
- **Regressão/risco**: re-login manual (2FA); Chrome+Xvfb em LXC é onde a issue 005 esperava
  dor ("mais estável numa VM completa") — **não medido**; RAM do host (7.5G total, 5.3G
  available) com o CT limitado a 3G.
- **Rollback**: o perfil e o service do host ficam intactos até o CT ser provado. Reverter =
  repontar o `ai-hub.conf` para `127.0.0.1:9400` e reativar o service do host.

## ARO

- **Acceptance**: os 4 itens de "Comportamento esperado", com o Chrome **com** sandbox.
- **Risk**: sandbox não subir no CT (→ linha vermelha); sessão não voltar no re-login;
  guardian continuar cuidando do daemon errado (do host) e mascarar falha do CT.
- **Operations**: **APPLY GATEADO.** Todo passo no stage4 exige aprovação explícita do
  operador. Nada de `--yes`. O recon desta issue foi read-only.

## DoD

- Hub rodando no CT 4001 com sandbox; host sem Chrome/Xvfb/CDP; `health()` UP do devel3 sem
  mudar o Gateway; guardian apontando para o CT; decisão A registrada; rollback testado ou
  documentado; napkin atualizado.

## Fora de escopo

- **Mover o Gateway** (decisão do operador: fica no devel3 — quer-se exercitar exatamente
  gateway-local ↔ hub-remoto).
- O **Postgres rodando dentro do CT 4001** (`127.0.0.1:5432`): sobra do desenho original
  (um LXC com Gateway + hub + banco). Com o Gateway ficando no devel3, está ocioso. Avaliar
  remoção em issue própria — não mexer aqui.
- Fechar o CT para a LAN (firewall do Proxmox) — ver Decisão B.
