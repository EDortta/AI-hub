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

1. ~~**Tornar o bind do daemon configurável**~~ — **FEITO** (2026-07-16, commit `6f81bc7`).
   `AIHUB_BIND_HOST`, default `127.0.0.1`, valor vazio cai de volta para loopback.
   6 testes em `tests/test_bind_host.py`. Era a única parte de código desta issue e não
   toca no stage4: sem a env, nada muda. **Já pode ser implantado sozinho, hoje.**
2. **Instalar no CT 4001**: `google-chrome-stable`, dependências do `requirements.txt`,
   o `chrome-daemon` (via `install/install.sh`, que já deriva paths de `$DAEMON_DIR`),
   usuário dedicado `ai-hub` + `loginctl enable-linger`, systemd user service.
3. **Validar o sandbox**: Chrome sobe **sem** `--no-sandbox` dentro do CT unprivileged.
   *Se não subir, PARE* — ver "Linha vermelha".
4. **Re-login no ChatGPT** via `x11vnc -display :99 -localhost` + túnel SSH. **Passo humano,
   com 2FA.** O perfil **não** migra (napkin do repo: ChatGPT/Google invalidam a sessão por
   mudança de IP/fingerprint).
5. **Repontar o nginx do host**: `proxy_pass` → `http://192.168.7.201:9400` (o IP fixo do
   CT) + `proxy_set_header Host localhost`. Ver a seção da decisão abaixo.
6. **Limpar o host**: parar/desabilitar o service do `ai-hub`, remover o usuário e o
   `chrome-profile` do hypervisor. **Só depois do CT provado** — o perfil do host é o
   rollback até lá.
7. **Guardian**: `/usr/local/sbin/ai-hub-guardian.sh` + `/etc/cron.d/ai-hub` passam a
   checar o daemon **no CT** (`pct exec 4001 -- systemctl --user -M ai-hub@ ...` ou o
   health via nginx). Hoje reiniciam o do host.

## Como o nginx do host alcança o daemon — **decidido** (operador, 2026-07-16)

**O CT tem IP fixo; o nginx do host redireciona para ele.** Uma versão anterior desta issue
propunha um segundo nginx *dentro* do CT para manter o daemon em loopback. Foi
over-engineering: o `:9400` já vive hoje atrás de nginx com o mesmo token, então o hop extra
não compra segurança — compra um componente a mais para manter.

O IP **já está fixo**, não há o que fazer: `pct config 4001` →
`net0: ... ip=192.168.7.201/24, gw=192.168.7.6` (estático, não `dhcp`) e `onboot: 1`.

```nginx
# /etc/nginx/sites-available/ai-hub.conf (host) — só o upstream muda
listen 9480;
server_name 192.168.7.200 stage4;
proxy_pass http://192.168.7.201:9400;   # era http://127.0.0.1:9400
proxy_set_header Host localhost;        # ver "Host-allowlist" abaixo
proxy_set_header Authorization $http_authorization;
```

### O que isto exige do daemon (mudança de código, pequena)

**O bind é hardcoded**: `chrome-daemon/main.py:794` → `host="127.0.0.1"`. Dentro do CT, o
loopback do CT não é o do host, então o nginx do host **não alcança** um daemon em
`127.0.0.1`. Precisa virar configurável:

```python
# main.py — aditivo e fail-safe: sem a env, o comportamento é o de hoje.
host=os.getenv("AIHUB_BIND_HOST", "127.0.0.1"),
```

`AIHUB_BIND_HOST=0.0.0.0` no `daemon.env` do CT. **Default inalterado** → nenhum deployment
existente muda de comportamento (`design-standards.md` §4: campo novo, opcional, aditivo).

### Host-allowlist — e o que ela *não* protege

O daemon valida o `Host` contra `{127.0.0.1, localhost, ::1}` + `AIHUB_ALLOWED_HOSTS`
(`main.py:109`). Com `proxy_set_header Host localhost`, o nginx passa e a allowlist segue
funcionando sem alargá-la. Um chamador direto em `192.168.7.201:9400` mandando
`Host: 192.168.7.201` toma **403**.

**Mas seja honesto sobre o que isso vale**: forjar um header `Host` é trivial. A allowlist é
quebra-molas contra DNS-rebinding e engano acidental, **não** contra um atacante deliberado
na LAN. Quem protege o `:9400` é o **token fail-closed** (SEC-0001) — e ele já é o que
protege o `:9480` hoje. Consequência: **o `:9400` do CT fica alcançável da LAN**, com o mesmo
nível de proteção que o `:9480` já tem hoje. Não é regressão; é a mesma superfície, mudando
de porta. Fechar isso para a LAN é firewall do Proxmox — ver "O CT tem IP na LAN", escopo separado.

## Rede — o CT está na LAN, e a convenção da caixa diz que não deveria

**Decisão do operador (2026-07-16): IP interno à caixa, com o nginx do host como porta
única.** Está certo, e é a convenção que a própria caixa já segue — o 4001 é o fora-da-curva:

| CT | bridge | IP | precisa de internet? |
|---|---|---|---|
| 1001 dnsmasq | vmbr2 | 192.168.1.2 | não |
| 2001 rsyslog | vmbr2 | 192.168.1.3 | não |
| 3001 mosquitto | vmbr2 | 192.168.1.4 | não |
| **4001 ai-ecosystem** | **vmbr0** ← anomalia | **192.168.7.201** (LAN) | **SIM** (chatgpt.com) |

`vmbr2` = `192.168.1.0/24`, host em `192.168.1.1`. `vmbr0` = LAN (host em `192.168.7.200/24`
**e** `192.168.71.200/24` — é por aí que o devel3, em `192.168.71.6`, alcança).

### O bloqueio: vmbr2 não tem saída para a internet (medido, 2026-07-16)

```
ip route get 1.1.1.1 from 192.168.1.2   →  RTNETLINK: Network is unreachable
pct exec 1001 -- ping -c1 1.1.1.1       →  100% packet loss
pct exec 1001 -- getent hosts chatgpt.com →  (nada)
pct exec 4001 -- getent hosts chatgpt.com →  resolve  (está no vmbr0)
```

A regra `-A POSTROUTING -s 192.168.1.0/24 -o vmbr0 -j MASQUERADE` existe e `ip_forward=1`,
mas a **rota default do host é pelo wifi** (`default via 192.168.15.1 dev wlxd037453dacfd
metric 50`, com `vmbr0` só em metric 200). O tráfego do vmbr2 não sai por vmbr0, então não é
mascarado — e morre.

**Nunca deu na vista** porque nenhum CT do vmbr2 precisa de internet: dnsmasq, rsyslog e
mosquitto são internos. O hub é o primeiro que precisa. Mover o 4001 para vmbr2 hoje, sem
mais nada, **mata o hub**.

### Duas saídas (decisão pendente)

**R1 — Consertar a saída do vmbr2.** Masquerade também na interface do wifi (ou acertar a
rota). Fica limpo e beneficia qualquer CT futuro.
*Risco:* mexe em **roteamento/NAT do host** — a caixa que roda DHCP (o próprio dnsmasq do
vmbr2!), OpenVPN e o túnel reverso. É exatamente a categoria de mudança que a epic 004
declarou intocável. Precisa de janela e rollback pensado.

**R2 — CT dual-homed, com o daemon amarrado à interface interna.**
`net0` = vmbr2 `192.168.1.5` (o nginx do host alcança por aqui) · `net1` = vmbr0 (só para a
saída do Chrome; **nenhum serviço escuta nela**).
`AIHUB_BIND_HOST=192.168.1.5` → o daemon **não** faz bind na interface da LAN. O `:9400` fica
inalcançável da rede, que é o objetivo, **sem tocar no roteamento do host**.
*Atenção:* o `sshd` do CT faz bind em `*:22` (todas as interfaces) — ou se amarra também, ou
se aceita ssh pela LAN, ou firewall do Proxmox no `net1`.

### Decidido: **R1** (operador, 2026-07-16), e ele é mais barato do que eu avisei

Diagnóstico fechado depois de medir: **R1 não mexe em roteamento.** Descartados FORWARD
(`-P ACCEPT`), `rp_filter` (all=2, vmbr2=0) e `ip_forward` (=1). A causa é uma linha:

```
/etc/network/interfaces:31
post-up iptables -t nat -A POSTROUTING -s 192.168.1.0/24 -o vmbr0 -j MASQUERADE
                                                          ^^^^^^^^ mas a default é o wifi
```

A regra masquerada só a saída pela `vmbr0`; a rota default da caixa é
`wlxd037453dacfd metric 50`. O pacote sai pelo wifi, não casa a regra, e vai embora com origem
privada. Conserto = **uma regra aditiva** (`! -o vmbr2` em vez de `-o vmbr0`), reversível com
um `-D`, numa faixa que hoje não sai de jeito nenhum. Pior caso: continua como está.

Isso vive em issue própria, **fora deste repo** porque é infra da caixa e não do hub:
`~/Sync/Projects/stage4-hardening/issues/001-vmbr2-sem-saida-para-internet-[draft].md`.
**Ela bloqueia esta 008**: sem saída no vmbr2, hub no vmbr2 é hub sem chatgpt.com.

Com R1 feito, esta issue fica simples: `net0` do 4001 vai para **vmbr2 `192.168.1.5`**, o
daemon segue em `AIHUB_BIND_HOST=0.0.0.0` (agora só há interface interna), e o nginx do host
faz `proxy_pass http://192.168.1.5:9400` + `proxy_set_header Host localhost`. O `:9400` deixa
de existir para a LAN — por construção, não por regra.

> R2 (CT dual-homed com o daemon amarrado à interna) fica registrado como **plano B** se o R1
> esbarrar em algo na hora. O `AIHUB_BIND_HOST` aceita interface específica e tem teste.

### Epic guarda-chuva

Esta issue é o **piloto** de `~/Sync/Projects/stage4-hardening/epic.md` — tirar da baremetal
do stage4 tudo que é serviço exposto. Ela prova o padrão (Chrome com sandbox em CT
unprivileged; CT no vmbr2; nginx do host como porta única) que a `004` daquela epic vai
aplicar no `zeecred-sftp` — um app de upload rodando **como root** no hypervisor, caso ainda
mais forte que o nosso.

## Linha vermelha (não negociável)

**Se o Chrome não subir sem `--no-sandbox` dentro do CT, pare e reavalie — não desligue o
sandbox.** O container protege o *host*; o sandbox protege o *perfil logado* de um renderer
comprometido. Trocar um pelo outro não é ganho, é troca lateral — e o perfil é o ativo mais
caro (2FA manual para recriar). `nesting=1` já está ligado justamente para isto.
Ver `security-standards.md` §3: "flags que desligam proteções nunca são default".

## PROVADO (2026-07-16) — Chrome roda COM sandbox no CT unprivileged

A linha vermelha era o risco que podia matar esta issue. **Passou, com evidência**, não com
"não deu erro". Chrome 150.0.7871.128 instalado no CT 4001, rodando como `ai-hub` (não root),
sob Xvfb `:99`, **sem `--no-sandbox`**:

```
browser=28932  zygote=28940  renderer=29008        usuário: ai-hub
  user  ns: browser=4026532428  renderer=4026532596   DIFERENTE → isolado
  pid   ns: browser=4026532432  renderer=4026532679   DIFERENTE → isolado
  net   ns: browser=4026532435  renderer=4026532605   DIFERENTE → isolado
  chroot  : renderer em /proc/28943/fdinfo  (layer-1 sandbox)
  seccomp : Seccomp: 2 / Seccomp_filters: 2  (filtro BPF ativo)
CDP: {"Browser": "Chrome/150.0.7871.128", "Protocol-Version": "1.3"}
```

Todas as camadas do sandbox do Chrome estão ativas dentro do LXC unprivileged. O
`features: nesting=1` entrega o que prometia. **Não haverá troca lateral**: ganhamos a
fronteira do container **e** mantemos a do renderer.

**Erro meu no caminho, registrado porque ensina:** o primeiro teste rodou Chrome como root e
falhou com `Running as root without --no-sandbox is not supported`. Não era o sandbox
falhando — era o Chrome se recusando a rodar como root, que é *exatamente* o comportamento
correto e a razão de SEC-0107 usar usuário dedicado. O teste estava errado, não o desenho.
Quem for validar isso de novo: **teste como `ai-hub`, nunca como root**, ou vai concluir a
coisa errada.

## Comportamento esperado

- `AiHubDriver.health()` **do devel3** → `('UP', None)` contra `http://192.168.7.200:9480`,
  igual a hoje — **o Gateway não muda de config** (decisão do operador: Gateway local, hub
  remoto; é justamente essa conexão que se quer exercitar).
- `ps -eo user,comm | grep -E "chrome|Xvfb"` **no host** → vazio.
- `ss -tlnp | grep 9222` **no host** → vazio.
- Geração de imagem e `/conversations/*/send` funcionando de dentro do CT.

## EXECUTADO (2026-07-16) — migração feita, falta **só o seu login**

Aprovado por "faz tudo" (operador, 2026-07-16). Feito e verificado:

| Passo | Estado |
|---|---|
| `AIHUB_BIND_HOST` configurável | ✅ commit `6f81bc7`, e **em produção**: daemon do CT escuta `0.0.0.0:9400` |
| Chrome no CT **com sandbox** | ✅ provado (ver seção acima) |
| Hub instalado no CT 4001 | ✅ código + deps + unit systemd user + linger + Xvfb como serviço |
| CT movido para a rede interna | ✅ `vmbr0/192.168.7.201` → **`vmbr2/192.168.1.5`** |
| Perfil do Chrome (3.4G) copiado | ✅ |
| nginx do host repontado | ✅ `proxy_pass http://192.168.1.5:9400` + `Host: localhost`; `nginx -t` OK, **reload** (não restart) |
| Host limpo | ✅ daemon `disable --now`; **zero** Chrome/Xvfb/CDP fora de CT |
| Guardian + process_monitor | ✅ repontados para o CT |
| **Login no ChatGPT** | ❌ **SEU** — 2FA, ver abaixo |

### Verificação final

```
Gateway (devel3, config INALTERADA) → AiHubDriver.health() → ('UP', None)
192.168.1.5:9400 do devel3 → inalcançável   (só via nginx :9480 — porta única)
192.168.7.201:9400          → morto         (o CT saiu da LAN)
Chrome fora de CT no host   → 0             (cgroup de todos: /lxc/4001/...)
                              pid ns do Chrome 4026532432 ≠ host 4026531836
                              uid visto do host: 101000 (mapeamento unprivileged)
CDP :9222 no host           → 0
Infra intocada: /enviar-arquivo/ 200 · 4 CTs running · portas expostas: 22 80 3128 5055 8006 9480
```

### O que falta: o login (2FA — só você faz)

**A sessão do ChatGPT não sobreviveu à cópia do perfil** — a lição do napkin vale mesmo para
container na mesma caixa (`logged_in: false` no CT).

**Mas ela já estava morta no host antes de eu encostar em nada**: o daemon do host, religado
para conferir, também reportou `chatgpt_logged_in: false`. O hub estava sem conseguir fazer o
trabalho dele há algum tempo. O re-login **não é custo desta migração** — é dívida que já
existia, e agora um login só resolve as duas coisas.

```bash
# no CT, expor o :99 por VNC (só loopback):
ssh ai-ecosystem 'x11vnc -display :99 -localhost -nopw -forever &'
# do devel3, tunelar e abrir o cliente VNC em localhost:5900:
ssh -L 5900:127.0.0.1:5900 ai-ecosystem
# depois:  curl -H "Authorization: Bearer <token>" http://192.168.7.200:9480/session/check
```

### Rollback (intacto até você mandar apagar)

O perfil e o checkout do `ai-hub` **continuam no host**, e a unit está apenas
`disable`, não removida. Reverter:

```bash
systemctl --user -M ai-hub@ enable --now chrome-daemon.service
cp /etc/nginx/sites-available/ai-hub.conf.bak-20260716 /etc/nginx/sites-available/ai-hub.conf
nginx -t && systemctl reload nginx
cp /etc/cron.d/ai-hub.bak-20260716 /etc/cron.d/ai-hub
```

Limpar o host (`userdel ai-hub`, apagar o perfil de 3.4G) é passo separado, **depois** de o
login provar o CT. Item 6 do escopo fica aberto por isso.

### Achados do caminho (nenhum estava previsto)

1. **`psutil` não estava instalado no CT.** O `_kill_stale_chrome` do watchdog importa psutil
   e, sem ele, **retorna 0 em silêncio** — o watchdog inteiro vira no-op. Instalado. Vale
   como lição: dependência de um guard que falha aberta e calada é um guard que não existe.
2. **Copiei o código do host, não do repo.** O `main.py` novo importava `JsonFileWatcherStore`
   de um `watchers.py` velho → `ImportError` em loop. Resolvido enviando o `chrome-daemon`
   inteiro do repo (o que **implantou o trabalho de 2026-07-16**: watchdog, persistência,
   cobertura do guard — 48 testes verdes).
3. **Os CTs de infra são Alpine**, não Debian: sem `systemd`, sem `apt`. O script de ssh teve
   de ser refeito com `apk`/`rc-service`.
4. **`pgrep -c chrome` no host conta os processos do CT** — o LXC compartilha o `/proc` do
   host. Só o `cgroup`/namespace distingue. Quem for auditar isso depois: **não use `pgrep`
   para concluir "o host está sujo"**, use `grep lxc /proc/<pid>/cgroup`.

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
- Fechar o CT para a LAN (firewall do Proxmox) — ver "O CT tem IP na LAN".
