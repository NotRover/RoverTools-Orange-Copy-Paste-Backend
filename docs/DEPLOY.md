# RoverTools' Orange Copy Paste — Backend Deployment

**Owns:** the self-hosted OVH VPS that runs the sync backend and Redis — how to reach it,
recover it, harden it, and deploy the API onto it (Docker Compose + Caddy, deployed by a
git poll on the box). Also
the external services the backend depends on (Supabase, R2) and the migration discipline.
**Not here:** the wire contract — routes, payloads, DDL, socket events, crypto envelope —
which is [architecture.md](architecture.md); client internals (the app's own
`docs/architecture.md`); who-may-do-what (`docs/permissions.md` at the workspace root).

**Every host, domain, IP, and key value in this file is a placeholder** — `example.com`,
`203.0.113.10` / `2001:db8::1`, `your-vps-hostname`, `SHA256:REDACTED-HOST-KEY`, and the
like. Replace them with your own. Passwords and keys are named by *where they live*, never
by value; real secrets belong in the untracked `.env` and your password manager, never in
this file or in git. Anything pasted into git history stays there forever — if a real
secret ever lands here, rotate it, do not just edit it out.

> This is the deployment runbook for self-hosting the backend. The setup described here is
> the reference deployment the maintainers run — Docker Compose + Caddy on a small VPS,
> deployed by a git poll on the box; the specific host, domain, and keys are theirs and
> appear only as placeholders above. A friendlier, step-by-step version for newcomers is
> on the docs site. It replaced an earlier Render/Supabase/R2 setup.

## Table of Contents

**Part A — the box**
1. [The box](#1-the-box)
2. [Access — the part you cannot lose](#2-access--the-part-you-cannot-lose)
3. [Recovery — when SSH will not let you in](#3-recovery--when-ssh-will-not-let-you-in)
4. [What is hardened, and why](#4-what-is-hardened-and-why)
5. [Reproduce the box from scratch](#5-reproduce-the-box-from-scratch)

**Part B — the backend on the box**
6. [What you are deploying](#6-what-you-are-deploying)
7. [External services — Supabase and R2](#7-external-services--supabase-and-r2)
8. [Database migrations](#8-database-migrations)
9. [Deployment — build-on-box, polled from git](#9-deployment--build-on-box-polled-from-git)
10. [Verify the deployment](#10-verify-the-deployment)
11. [Point the desktop app at it](#11-point-the-desktop-app-at-it)
12. [Ongoing operations](#12-ongoing-operations)
13. [Troubleshooting](#13-troubleshooting)
14. [Decisions — settled and open](#14-decisions--settled-and-open)
15. [History](#15-history)

---

# Part A — the box

## 1. The box

| | |
|---|---|
| Provider | OVHcloud VPS-1 |
| Hostname | `your-vps-hostname` |
| IPv4 | `203.0.113.10` |
| IPv6 | `2001:db8::1` |
| Domain | `api.example.com` (FreeDNS) -> `203.0.113.10`; status at `status.example.com` |
| OS | Ubuntu 26.04 LTS (resolute) |
| Size | 2 vCPU - 3.7 GiB RAM - 38 GB disk, plus a 2 GB swap file (section 5.9) |
| Timezone | UTC |
| Admin user | `ubuntu` (passwordless `sudo`) |
| `root` | locked — no root login by any path, including the console |
| Deploy runs as | `ubuntu` in `~/app`, pulling read-only over HTTPS with a fine-grained token (section 5.6) |

---

## 2. Access — the part you cannot lose

### Normal login

From the Windows workstation:

```
ssh ubuntu@203.0.113.10
```

- Auth is **key only**. Password login over SSH is disabled.
- The private key is `~/.ssh/id_ed25519`, protected by a passphrase. SSH
  prompts for that passphrase on connect (or once per session if `ssh-agent` is running).
- Public key fingerprint: `SHA256:REDACTED-HOST-KEY`
  (comment `you@example.com`). This is the only key in `ubuntu`'s `authorized_keys`.
- To skip the passphrase prompt every connection, load the key into `ssh-agent` once: in an
  **admin** PowerShell `Set-Service ssh-agent -StartupType Automatic; Start-Service
  ssh-agent`, then in a normal one `ssh-add $HOME\.ssh\id_ed25519`. Holds until reboot.

### Two secrets, do not confuse them

These are unrelated, and mixing them up wasted a session once:

| | Where it lives | What it unlocks | Prompt you see |
|---|---|---|---|
| **Key passphrase** | the workstation, on the key file | the private key, so SSH can use it | `Enter passphrase for key '...id_ed25519'` |
| **`ubuntu` account password** | the server (`/etc/shadow`) | the OVH console, and `sudo` if `NOPASSWD` is ever removed | the OVH console login, never SSH |

**`passwd` changes only the account password. It has zero effect on SSH login**, because
SSH here authenticates by key, not by account password. If a login prompt on your
workstation says "passphrase for key", it wants the key passphrase — typing the account
password there just fails. Change the account password freely; it never touches SSH.

**If the workstation or that key is lost, the key alone cannot get you in again** — you add
a new key through the recovery path below. Keep the `ubuntu` password (in the password
manager) safe: it is not an SSH path, but it is the console path, and the console is how you
install a replacement key.

### Verifying you are talking to the real box

On a first connection, or if SSH ever warns about a changed host key, the fingerprint must
match one of these (captured and verified out-of-band on 2026-08-24):

```
ED25519  SHA256:REDACTED-HOST-KEY
ECDSA    SHA256:REDACTED-HOST-KEY
RSA      SHA256:REDACTED-HOST-KEY
```

A mismatch means either the box was rebuilt or someone is between you and it. Do not type a
passphrase; find out which first.

---

## 3. Recovery — when SSH will not let you in

Ordered from least to most drastic. Try them in order.

### 3a. OVH KVM console (the main escape hatch)

Works even when sshd, the firewall, or the network config is broken, because it is a
virtual monitor and keyboard, not a network service.

1. OVH panel -> Bare Metal Cloud -> VPS -> this VPS -> actions menu (`...`) -> **Console**.
2. Log in as `ubuntu` with its password (password manager). `root` is locked, so it is not
   an option here.
3. Fix whatever broke, then verify over SSH from a *new* terminal before trusting it.

Confirm this console actually accepts the `ubuntu` password **while SSH is healthy**, not
for the first time during an outage. An untested escape hatch is not an escape hatch.

### 3b. Restore a lost admin key

If the workstation key is gone, get in via the console (3a), then append a new public key
and — because SSH is locked to named users — make sure the account is still allowed:

```
echo 'ssh-ed25519 AAAA...newkey... comment' >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
# only if you add a NEW user rather than reusing `ubuntu`:
sudo sed -i 's/^AllowUsers .*/& newuser/' /etc/ssh/sshd_config.d/01-hardening.conf
sudo sshd -t && sudo systemctl restart ssh.socket
```

### 3c. OVH rescue mode (last resort)

If the console itself is unusable, boot the VPS into OVH rescue mode from the panel. It
brings up a temporary system with the real disk unmounted; mount it by hand to repair
`/etc/ssh`, `authorized_keys`, or a broken `/etc/fstab`. Slowest path — everything above
exists to avoid needing it.

### The golden rule for every change to SSH or the firewall

**Keep the working session open. Prove the change in a brand-new session before you close
the first one.** sshd here is socket-activated (`ssh.socket`), so each connection is its own
process and a bad config cannot kill the session you are sitting in — it only breaks *new*
logins. That open session is your safety line; do not sever it until a fresh login succeeds.

---

## 4. What is hardened, and why

Everything here is **done and verified**. The exact commands to rebuild it are in section 5.

### SSH — key-only, root-off, named users

Drop-in `/etc/ssh/sshd_config.d/01-hardening.conf`:

```
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
AuthenticationMethods publickey
MaxAuthTries 3
LoginGraceTime 20
X11Forwarding no
AllowUsers ubuntu
```

- **`01-` prefix, not `99-`.** `sshd_config` is first-match-wins and the
  `sshd_config.d/*.conf` include sits at the top of the file, so drop-ins are read in
  alphabetical order and the *earliest* to set a keyword wins. The stock files here are
  `50-cloud-init.conf` (which shipped `PasswordAuthentication yes`) and
  `60-cloudimg-settings.conf`. A `99-` file loses every conflict silently and looks like it
  did nothing.
- **`KbdInteractiveAuthentication no` matters as much as `PasswordAuthentication no`** —
  leaving PAM keyboard-interactive on is a live password path that makes you *think* you are
  key-only when you are not.
- **`AllowUsers ubuntu`** turns every username-guessing attempt into an instant reject. Only
  `ubuntu` logs in over SSH; `deploy` is a local service account that polls git and needs no
  inbound access (section 9). Any user you add later must be added to this line too (see 3b).
  (The box currently still lists `deploy` here from the earlier push-deploy design — drop it.)
- Port stays 22. Moving it only cuts log noise, and on socket-activated sshd the port lives
  in `ssh.socket`, not `sshd_config`, so editing the obvious file changes nothing.

### Firewall — deny by default

`ufw`: default deny incoming, allow outgoing, with only 22/80/443 open on both IPv4 and
IPv6. `IPV6=yes` is set — the box has a public IPv6 and sshd listens on `[::]:22`, so an
IPv4-only firewall would leave v6 wide open.

**Nothing opens 6379.** Redis runs inside the compose network with no published port; the
firewall is the second lock on a door that is not there. ufw on the box, not OVH's edge
firewall — the edge one is stateless with rule-count limits and easy to half-configure into
a break.

### Brute-force + patching

- **fail2ban**, `/etc/fail2ban/jail.local`: `backend = systemd`, `banaction = ufw`, 1h ban
  after 5 fails in 10m, `sshd` jail on.
  - `backend = systemd` is load-bearing: Ubuntu has not shipped rsyslog by default since
    24.04, so `/var/log/auth.log` never appears and the stock jail watches a file that does
    not exist. Reading the journal is the only thing that works.
  - `banaction = ufw` because the default `iptables-multiport` writes rules that fight the
    ufw chains.
  - With password auth off this is mostly log hygiene, but it works: it banned a real
    scanner within seconds of starting. Public port 22 gets constant automated probing; a
    nonzero `Total banned` is normal background noise, not a targeted attack.
  - **It can ban *you*** — 5 failed auths in 10 minutes from any IP, including yours, is a
    1h ban. Repeatedly fat-fingering the key passphrase can trip it. Symptom: SSH times out
    or is refused *before* the passphrase prompt (a ban blocks the TCP connection, so if you
    still get the prompt you are not banned). Check and recover:
    ```
    echo $SSH_CONNECTION                 # first field = your current client IP
    sudo fail2ban-client status sshd     # is your IP in the banned list?
    sudo fail2ban-client unban --all     # clear all bans (run from the KVM console if SSH is refused)
    ```
    Only unban an IP you have confirmed is yours; leave scanner bans in place.
- **unattended-upgrades** on, with `Automatic-Reboot "true"` at **04:30 UTC**
  (`/etc/apt/apt.conf.d/52unattended-reboot.conf`). Security origins include
  `resolute-security` and ESM. That nightly reboot briefly drops every WebSocket sync
  connection; clients reconnect on their own. Narrow it to reboot-only-when-required if that
  blip ever matters.

### Docker + who runs the deploy

- **Docker** — Ubuntu's own packages (`docker.io 29.1.3`, `docker-compose-v2 2.40.3`), not
  Docker's apt repo. Chosen so security patches flow through the unattended-upgrades already
  set up, with no third-party repo/key to maintain. `ubuntu` is in the `docker` group.
  `docker-buildx` deliberately left out — the plain builder is enough for the on-box build.
- **Runs as `ubuntu`** — the login user owns the checkout (`~/app`) and runs the deploy
  timer. Because deploys are **polled**, nothing logs in to deploy, so a dedicated service
  account buys little; the box reaches **out** to GitHub with an **outbound, read-only**
  fine-grained token over HTTPS (section 5.6), and nothing reaches in. `.env` lives inside
  the checkout, git-ignored, mode 600 (a `git reset --hard` leaves ignored files alone).
  - **Historical:** earlier designs added a separate `deploy` user — first with an inbound,
    forced-command-locked CI key (`id_ci`) for a GitHub Actions push-deploy, then as a no-SSH
    service account. Both were dropped for the simpler "run as `ubuntu`, poll git" model. On a
    box carrying that user, remove it: `sudo userdel -r deploy`, drop `deploy` from
    `AllowUsers`, `sudo rm -rf /opt/rovertools`.

---

## 5. Reproduce the box from scratch, step by step

This is the actual path taken on 2026-08-24, mistakes included — each step with how to
**verify** it before moving on, and the thing that **bit us** where one did. Run the steps
in order; the order is load-bearing (installing the key before disabling passwords is the
whole reason step 5.1 comes before 5.2). The reasoning behind each hardening choice is in
section 4; this section is the doing.

**The golden rule applies to every SSH/firewall step:** keep the working session open, and
prove the change in a brand-new session before closing it (section 3).

**The whole path, in order.** Everything below plus the stack sections, so a rebuild is one
list rather than a hunt. Nothing here is optional except where marked:

| # | Step | Section |
|---|---|---|
| 1 | First access from the OVH panel | 5.0 |
| 2 | Install and **prove** your SSH key, before any lockdown | 5.1 |
| 3 | SSH lockdown: key-only, no root, named users | 5.2 |
| 4 | Patch, then firewall (deny by default) | 5.3 |
| 5 | Brute-force protection + unattended upgrades | 5.4 |
| 6 | Docker, and the user that runs the deploy | 5.5 |
| 7 | Read-only pull token for the repo (HTTPS) | 5.6 |
| 8 | DNS: both hostnames, before first deploy | 5.7 |
| 9 | Persistent, capped journal for container logs | 5.8 |
| 10 | Swap file (2 GB, `nofail`, swappiness 10) | 5.9 |
| 11 | Supabase and R2 (external, unchanged by a rebuild) | 7 |
| 12 | `.env` on the box, mode 600 | 9, Secrets |
| 13 | Clone, systemd deploy timer, first deploy | 9 |
| 14 | Apply migrations deliberately - the deploy never does | 8 |
| 15 | Verify: TLS, healthz body, `via: 1.1 Caddy` | 10 |
| 16 | Metrics auth hash + Discord webhook into `.env` | 12 |
| 17 | Point the desktop app at it | 11 |

Steps 1-10 build the box; 11-17 put the backend on it. The monitoring stack needs no manual
setup beyond step 16: Netdata's collector and notification config live in `netdata/conf/` in
the repo and the deploy installs them, so a rebuilt box arrives already watching itself.

### 5.0 First access (OVH)

Provision VPS-1 with Ubuntu 26.04. OVH creates the `ubuntu` account with **passwordless
sudo** and leaves `root` **locked** — you never log in as root, by any path. The first
login is the one and only time you use a password over SSH (key auth is not set up yet):

```bash
ssh ubuntu@<ip>            # password auth is still on at this point
sudo passwd ubuntu         # set the account password, store it in your password manager
```

That account password is the **console/sudo** secret, unrelated to any SSH key (section 2).
Confirm two things now, while everything still works:

```bash
sudo -k; sudo -n true && echo "NOPASSWD sudo: ok"   # sudo must not need a password
```

- Open the OVH panel -> this VPS -> `...` -> **Console** and confirm the KVM console
  accepts the `ubuntu` password. This is your escape hatch (section 3a); test it before you
  need it, not during an outage.
- **What bit us:** we assumed key auth was already configured. It was not — every early
  login was by password and `ubuntu`'s `authorized_keys` was empty. That is exactly what
  makes 5.1-before-5.2 non-negotiable.

### 5.1 Install and prove your SSH key (before any lockdown)

From the Windows workstation. `~` does not expand in some Windows shells, so use the
absolute path to the key:

```powershell
# workstation (PowerShell): print the PUBLIC key, then paste it into the box's authorized_keys
type ~/.ssh/id_ed25519.pub
```

```bash
# on the box:
mkdir -p ~/.ssh && chmod 700 ~/.ssh
echo 'ssh-ed25519 AAAA... you@example.com' >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

Record the host-key fingerprints now so a future "changed host key" warning is decidable
(section 2):

```bash
ssh-keyscan <ip> 2>/dev/null | ssh-keygen -lf -
```

**Verify — do not proceed until this passes:** from a *new* terminal, `ssh ubuntu@<ip>`
logs in **by key**. The prompt it shows is `Enter passphrase for key` (the key passphrase),
not a password. If it still asks for a password, the key is not installed correctly.

### 5.2 SSH lockdown

Only after 5.1 proves key login. The `01-` prefix matters (it beats `50-cloud-init.conf`,
which ships `PasswordAuthentication yes`); so does `KbdInteractiveAuthentication no` (it
closes the PAM password path) — see section 4.

```bash
sudo tee /etc/ssh/sshd_config.d/01-hardening.conf >/dev/null <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
AuthenticationMethods publickey
MaxAuthTries 3
LoginGraceTime 20
X11Forwarding no
AllowUsers ubuntu
EOF
sudo sshd -t && sudo systemctl restart ssh.socket
```

**Verify:** open a *new* terminal and confirm `ssh ubuntu@<ip>` still works, then confirm
password auth is dead: `ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no
ubuntu@<ip>` must be refused. Only then close the original session. sshd is socket-activated
(`ssh.socket`), so a bad config only breaks *new* logins — your open session survives it.

**What bit us:** this is the step that locked us out the first time, because `authorized_keys`
was empty (5.0). Recovered by pasting the key in via the still-open session, then confirming
a fresh key login.

### 5.3 Patch, then firewall

```bash
sudo apt update && sudo apt full-upgrade -y && sudo apt autoremove --purge -y
# (reboot if the kernel was updated: sudo reboot)

sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp comment 'ssh'
sudo ufw allow 80/tcp comment 'http'
sudo ufw allow 443/tcp comment 'https'
sudo ufw --force enable
```

**Verify:** `sudo ufw status verbose` shows `deny (incoming)` and 22/80/443 open on **both**
v4 and `(v6)` — `IPV6=yes` in `/etc/default/ufw` (Ubuntu's default) is what gets the v6
rules, and the box has a public IPv6. From the workstation, `Test-NetConnection <ip> -Port
6379` must report `TcpTestSucceeded : False`.

### 5.4 Brute-force + auto-patching

```bash
sudo apt install -y unattended-upgrades fail2ban python3-systemd
sudo tee /etc/fail2ban/jail.local >/dev/null <<'EOF'
[DEFAULT]
backend = systemd
banaction = ufw
bantime = 1h
findtime = 10m
maxretry = 5

[sshd]
enabled = true
EOF
sudo systemctl restart fail2ban
sudo tee /etc/apt/apt.conf.d/20auto-upgrades >/dev/null <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
sudo tee /etc/apt/apt.conf.d/52unattended-reboot.conf >/dev/null <<'EOF'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:30";
EOF
```

**Verify:** `sudo fail2ban-client status sshd` returns a jail status (proves `backend =
systemd` is reading the journal — the stock file backend would fail here because modern
Ubuntu ships no `/var/log/auth.log`).

**What bit us:** fail2ban bans *any* IP with 5 failed auths in 10 minutes — **including
you**. Fat-fingering the key passphrase repeatedly can do it; the symptom is SSH refused
*before* the passphrase prompt. Unban from the KVM console with `sudo fail2ban-client unban
--all`. A scanner (198.51.100.20) was banned within seconds of starting the jail — that is
normal background noise on a public port 22, not a targeted attack.

### 5.5 Docker

Ubuntu's own packages (patched via the unattended-upgrades just set up), no `buildx` — the
box only pulls and runs; images build in CI.

```bash
sudo apt install -y docker.io docker-compose-v2
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu
newgrp docker            # activate the group in THIS shell; a fresh login does it permanently
docker run --rm hello-world
```

**Verify:** `docker run --rm hello-world` prints `Hello from Docker!`.

**What bit us:** running `docker ps` right after `usermod` gave `permission denied ...
docker.sock` — the new group is not active in the shell that added it. `newgrp docker` (or
logging out and back in) fixes it. Note `docker` group membership is root-equivalent
(section 4).

### 5.6 Read-only pull token (HTTPS)

The deploy runs as `ubuntu` (section 4) and only ever pulls, so the box needs read-only
outbound access to the private repo and nothing more.

**Why not a deploy key.** The original design used an SSH deploy key. The `NotRover` org
disables deploy keys by policy ("Disabled by NotRover" on the repo's Deploy keys page, no
per-repo override), so the box authenticates with a **fine-grained personal access token**
over HTTPS instead. The security profile is the same as the old key: read-only, one repo,
lives only on the box.

Create the token in GitHub (owner **NotRover** approves it, since it targets an org repo):

- Settings -> Developer settings -> Fine-grained tokens -> Generate new token.
- Resource owner **NotRover**; repository access limited to
  `RoverTools-Smart-Clipboard-App-Backend`; Repository permission **Contents: Read-only**.
- Fine-grained tokens must expire (max ~1 year). Set a reminder to rotate before then;
  an expired token makes every deploy poll fail on `git fetch` until it is replaced.

The token is embedded in the `origin` remote URL on the box (see section 9), so it lands in
`~/app/.git/config` — `chmod 600` that file. The rest of the wiring — cloning the repo,
`.env`, and the systemd timer — is in section 9, since it depends on the repo files.

**Historical:** an earlier design added a separate `deploy` user with an inbound,
forced-command-locked CI key (`id_ci`) for a GitHub Actions push-deploy. The poll model
dropped it. On a box provisioned under the old design, remove the whole account:
`sudo userdel -r deploy`, drop `deploy` from `AllowUsers`, `sudo rm -rf /opt/rovertools`.

### 5.7 Domain (FreeDNS)

The name Caddy gets its TLS cert for. At `freedns.afraid.org`, add an **A** record for a
subdomain pointing at the box's IPv4. The API is `api.example.com` -> `203.0.113.10`,
and the status dashboard is `status.example.com` -> the same box.

**Verify:** `nslookup api.example.com 8.8.8.8` returns `203.0.113.10`.

**What bit us:** the first record pointed at the wrong IP; fix the A record's destination in
the FreeDNS panel. DNS negatively caches, so a resolver queried too early (Cloudflare's
`1.1.1.1` did this) can hold a stale "no such name" for a while — check on the authoritative
nameserver or `8.8.8.8`. Harmless for TLS: Let's Encrypt validates from its own resolvers,
not a public cache.

### 5.8 Persistent, capped journal for container logs

The containers log to the systemd journal (`journald` driver, section 9), so the logs outlive
the container swaps a deploy makes. Two things to set once: make the journal persistent (write
to disk, not just RAM) and cap it so it can never fill the disk.

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
sudo tee /etc/systemd/journald.conf.d/rovertools.conf >/dev/null <<'EOF'
[Journal]
Storage=persistent
SystemMaxUse=500M
MaxRetentionSec=1month
EOF
sudo systemctl restart systemd-journald
```

**Verify:** `journalctl --disk-usage` reports a figure under the cap, and `ls /var/log/journal`
exists (persistent storage active). Without this, `Storage=auto` keeps logs in a volatile ring
buffer that a reboot wipes — the opposite of "read them anytime."

Container logs are written by the Docker daemon into the **system** journal, which an ordinary
user cannot read. So `journalctl -t rovertools-api` shows `-- No entries --` for `ubuntu` until
it can read the system journal — add it to the log groups (takes effect on next login):

```bash
sudo usermod -aG adm,systemd-journal ubuntu
```

Until you re-login, prefix reads with `sudo`. `sudo journalctl -t rovertools-api` always works.

---

### 5.9 Swap file

The VPS ships with **no swap at all** (`free -h` shows `Swap: 0B`). That is not a memory
problem - this box idles around 940 MB used of 3.7 GB with 2.8 GB available, and all five
containers together are under 300 MB - it is a *runway* problem. With no swap the kernel goes
straight from "fine" to the OOM killer choosing a victim, and the fattest target on this box
is the API container. 2 GB of swap on a 38 GB disk turns "the API is terminated mid-request"
into "things get briefly slow".

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
swapon --show && free -h
```

Make it survive a reboot. **`nofail` matters**: without it, a missing or corrupt swap file can
hold up boot, and this box has no console-free way back in if it does not come up (section 3):

```bash
echo '/swapfile none swap sw,nofail 0 0' | sudo tee -a /etc/fstab
sudo findmnt --verify --verbose | tail -5   # sanity-check fstab BEFORE trusting a reboot
```

Then tell the kernel to treat swap as an emergency reserve rather than something to use
eagerly - the default of 60 will swap out idle pages while RAM is free, which on a box with a
latency-sensitive API is the wrong trade:

```bash
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swappiness.conf
sudo sysctl --system | grep -i swappiness
```

Swap in use is a **signal, not a solution**. If Netdata starts showing swap consistently
occupied, something is genuinely growing and the answer is to find it, not to add more swap.

Redis is **not** installed on the host — it ships as a compose service (section 9). At this
point the box is hardened and deploy-ready; the application stack (Docker Compose + Caddy +
CI/CD) is section 9.

---

# Part B — the backend on the box

## 6. What you are deploying

Four moving parts. Only the first is code you ship; the last two are external and unchanged
by the move onto the VPS:

| Component | Runs on | Purpose |
|---|---|---|
| **FastAPI service** | VPS, Docker (`api`, N replicas) | The API + `/ws` realtime endpoint |
| **Redis** | VPS, Docker (`redis`) | WebSocket pub/sub fan-out + device presence |
| **Postgres + Auth** | Supabase (external) | Ciphertext store; issues the JWTs we verify |
| **Blob storage** | Cloudflare R2 (external) | Encrypted image/file blobs via presigned URLs |

Redis is **not optional** — realtime fan-out and presence depend on it. It holds only
ephemeral state (pub/sub + presence), so it needs no persistence.

The service is **stateless**: run N replicas freely. Cross-replica delivery goes through
Redis pub/sub, so a client connected to replica A still receives events published by
replica B. That is also what makes the rolling deploy in section 9 possible.

---

## 7. External services — Supabase and R2

Supabase owns identity and stores ciphertext; the backend only **verifies** its tokens and
never signs one. R2 stores encrypted blobs. Neither moved when the backend left Render.

### 7a. Supabase

Create a project, then collect four values.

> **Dashboard note:** Supabase reorganised these screens in 2025. API keys now live under
> **Settings -> API Keys** (also in the **Connect** dialog), and JWT configuration under
> **Settings -> JWT Keys**. Older guides pointing at "Settings -> API -> JWT Secret" are
> stale.

| Value | Where | Env var |
|---|---|---|
| Connection string (URI) | Settings -> Database | `DATABASE_URL` |
| Project URL | Settings -> API Keys | `SUPABASE_URL` |
| Secret key (`sb_secret_...`) | Settings -> API Keys | `SUPABASE_SERVICE_ROLE_KEY` |
| Legacy JWT secret | Settings -> JWT Keys | `SUPABASE_JWT_SECRET` *(usually blank — see below)* |

**Direct host vs pooler.** Supabase's direct host (`db.<ref>.supabase.co`) resolves to
**IPv6 only**. The VPS has outbound IPv6, so unlike the old Render host it *can* use the
direct host — but the **Supavisor pooler** (Connect dialog -> Session pooler) is still the
safer default: it is IPv4-reachable on every tier and does not depend on v6 routing staying
healthy. Rewrite the driver to asyncpg; the username carries the project ref:

```
postgresql+asyncpg://postgres.<project-ref>:<password>@aws-<region>.pooler.supabase.com:5432/postgres
```

| Mode | Port | Notes |
|---|---|---|
| **Session** (recommended) | 5432 | Behaves like a normal connection; the app already pools |
| Transaction | 6543 | Scales to more clients; **no prepared statements** |

Session mode fits — the app maintains its own SQLAlchemy pool. In transaction mode,
`database.py` detects port `6543` and disables asyncpg's statement caches automatically;
without that you would hit `prepared statement does not exist` under load.

**About the JWT secret — the part that trips people up.** Supabase has signed access tokens
with **asymmetric keys (ES256) by default since 2025-10-01**. The backend detects the
algorithm per token:

- **New project (2025-10-01 or later)** -> leave `SUPABASE_JWT_SECRET` **blank**. Tokens are
  verified against the project's JWKS endpoint, derived from `SUPABASE_URL`. Key rotation is
  picked up automatically, no redeploy.
- **Older project still signing HS256** -> set `SUPABASE_JWT_SECRET` to the legacy secret.
- **Mid-migration** -> both work at once; the JWKS carries the legacy secret alongside the
  new key.

`SUPABASE_SERVICE_ROLE_KEY` accepts either a new secret key (`sb_secret_...`) or the legacy
`service_role` key; Supabase deprecates the legacy keys at the **end of 2026**, so prefer a
secret key. It is server-only — never ship it to a client.

### 7b. Cloudflare R2

1. Create a bucket (e.g. `clipboard-blobs`) -> `S3_BUCKET`.
2. Create an R2 API token -> `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY`.
3. Copy the account endpoint -> `S3_ENDPOINT_URL`
   (`https://<account-id>.r2.cloudflarestorage.com`).
4. `AWS_REGION=auto`.

The bucket **must already exist** — the service only presigns URLs, it never creates
buckets. Text and note sync work without R2; only large image/file attachments need it.

---

## 8. Database migrations

**Migrate deliberately, and never take a green deploy as proof that anything migrated.** The
deploy pipeline (section 9) ships code only — it never runs a migration.

Nothing in the app's startup path migrates either, by design. So the revision is something
to read, never to infer.

### The Migrate database workflow

[`.github/workflows/migrate.yml`](../.github/workflows/migrate.yml) is the normal way to do
this, and it holds the production `DATABASE_URL` so nobody has to paste one.

It runs itself, read-only, on any push to `main` that touches `migrations/**`, and posts the
pending DDL to the run summary. Applying is always a separate, deliberate dispatch:

```bash
gh workflow run migrate.yml -f action=upgrade -f revision=head -f confirm=migrate
```

`action` has three values and only one of them writes:

| `action` | Does |
|---|---|
| `current` | Reports the revision and what is pending. Changes nothing. **The default** |
| `preview-sql` | Prints the SQL an upgrade would run. Changes nothing. |
| `upgrade` | Applies it. Also needs `confirm=migrate`, or it refuses. |

Every run reports where the database stands, and a read-only run that finds pending
revisions says so as a warning and prints the dispatch that would apply them — because the
failure worth designing against is a green run that looks like the fix and was not. An
`upgrade` re-reads the revision afterwards and fails if the database did not actually move.

The database is currently at **`0019`** (applied 2026-08-24).

### By hand

Equivalent, for a database the workflow does not hold credentials for:

```bash
uv sync
DATABASE_URL="postgresql+asyncpg://postgres:<pw>@db.<ref>.supabase.co:5432/postgres" uv run alembic upgrade head
```

PowerShell has no inline env prefix, so there it is two statements:

```powershell
$env:DATABASE_URL = "postgresql+asyncpg://postgres:<pw>@db.<ref>.supabase.co:5432/postgres"
uv run alembic upgrade head
```

`migrations/env.py` reads `DATABASE_URL` from the environment and ignores `.env`, falling
back to the localhost URL in `alembic.ini` when it is unset — so an unset variable migrates
your own machine, quietly. Check where you point before running, and read the applied
revision rather than inferring it:

```bash
uv run alembic current
```

`pytest` cannot catch a missing migration: the harness builds its schema with
`Base.metadata.create_all`, so a table can exist for every test and still be absent from a
real database.

---

## 9. Deployment — build-on-box, polled from git

**No GitHub Actions and no image registry, on purpose.** A private repo's Actions minutes
(2,000/mo) and — the real trap — its GHCR/Packages storage (500 MB free) both cost money the
moment builds accumulate. Render hid that by building on its own machines; the self-hosted
equivalent that spends nothing on GitHub is to build on the VPS you already pay for. GitHub's
only job is hosting the repo; the box pulls it read-only.

**Status: live on the box since 2026-08-24** (merged to `main`, deployed and verified —
section 15). The wiring below is what was done once; the repo files are the source of truth,
and this section explains them and the parts that live nowhere else.

### How a deploy flows

The box **reaches out** to GitHub; nothing reaches in. A systemd timer runs `deploy.sh` on a
~90s poll:

```
push to main ─▶ GitHub (repo only)
                   ▲  git fetch (read-only token over HTTPS, outbound)
                   │
   systemd timer ─▶ deploy.sh:  origin/main moved?
                                   ├─ no  → exit (quiet no-op, the common case)
                                   └─ yes → git reset --hard → docker compose build api
                                            → docker compose up -d  (recreate api)
```

- The image is **built on the box** from the checkout and tagged `rovertools-api:<short-sha>`
  (plus `:latest`). It never leaves the box — no registry, no push, no Actions.
- Deploys land within a couple of minutes of a push. No inbound endpoint, no webhook secret,
  no CI credentials on the box — the attack surface stays exactly "outbound git + Docker".
- Config travels with the code: `docker-compose.prod.yml`, `caddy/Caddyfile`, and `deploy.sh`
  all live in the checkout, so a change to any of them ships on the next poll like app code
  does. For the Caddyfile that takes a deliberate reload step in `deploy.sh` - see the trap
  below.

### What "zero downtime" means here

**There is none, and that is a deliberate choice.** There is one `api` container and a deploy
recreates it, so every deploy has a **~1-3s window with no backend**:

- **HTTP** — a request landing in that window gets a **502**. Measured, not assumed: a
  watch-curl across `deploy.sh --force` shows two 502s (one immediate, one ~3s dial timeout)
  and 200s either side. Clients retry, and deploys are infrequent, so it rarely meets a real
  request.
- **WebSockets** — open sockets drop once when the old container is removed, and clients
  reconnect. The presence heartbeat (`PING_INTERVAL = 25s`, `src/realtime.py`) keeps a socket
  under any 100s idle timeout and re-`SET`s presence on reconnect.

**What was tried and rejected.** True zero-gap needs two `api` containers overlapping, and
every route to that was declined:

- **`docker-rollout`** — a third-party single-file script running with Docker (root) access.
  Declined on trust. `deploy.sh` briefly guarded a `docker rollout` branch; the guard misfired
  once the plugin was absent (`docker <unknown> --help` exits 0, so the branch ran and failed
  the deploy with `unknown shorthand flag: 'f'`). The branch is gone — reintroducing overlap
  means editing `deploy.sh` deliberately.
- **Docker Swarm** — native start-first updates, but a cluster orchestrator on a single host,
  and `docker stack deploy` cannot build, which fights the build-on-box model.
- **Caddy `lb_try_duration` retry** — looked like a free win and **does not work here**. The
  config was confirmed live (`caddy adapt` showing `try_duration: 10000000000`) and the swap
  still 502'd identically. Retry/failover is for picking another *healthy host in a pool*; with
  one container the pool is momentarily empty and there is nothing to fail over to. Removed
  rather than left in place implying protection it does not give.

If the blip ever matters, the honest fix is overlap (Swarm, or a vendored+audited rollout
script), not proxy tuning.

### Architecture

```
                        VPS
   internet --> :443 --> Caddy --> api     --> Supabase (Postgres + Auth)  [external]
                         (TLS,       |  \------> Cloudflare R2 (blobs)      [external]
                          WS proxy)  |
                                     \--------> redis  (pub/sub + presence) [in-compose]
                                 \
                                  \----------> netdata (metrics dashboard)  [in-compose]

   api.example.com   -> api
   status.example.com -> netdata (basic auth)
```

- **Caddy** — the only container with published ports (80/443). Automatic TLS, proxies
  WebSockets with no config, serves both hostnames. Never rolled.
- **api** — built locally from the `Dockerfile`; **no** host port (only Caddy reaches it over
  the compose network). One replica; a deploy recreates it.
- **redis** — internal network, **no published port**, `requirepass`, persistence off. Not
  recreated on an `api` deploy, so presence is not needlessly flushed.
- **netdata** — metrics agent, **no** host port, behind Caddy basic auth. Reads the host
  read-only (`/proc`, `/sys`, `/`, `/var/log`) and gets container names from **dockerproxy**,
  a read-only allowlisted Docker socket proxy that is not web-facing.
  Watches the stack from inside it, which is why an external check still matters (section 12).

### The files (backend repo)

All under `orange-copy-paste-clipboard-backend/`. Read them for detail; the non-obvious parts:

- **`Dockerfile`** — builds from `uv.lock` with `uv sync --frozen` (exact locked versions —
  closes the reproducibility caveat in section 13), non-root user, `HEALTHCHECK` on
  `/internal/healthz`. That endpoint returns 200 whenever the process can serve (a degraded
  Redis shows in the body, not the status code — `src/admin/router.py:85`), the right
  liveness signal for a swap. `.dockerignore` keeps `.git`/`.venv`/tests/docs out of the
  build context.
- **`docker-compose.prod.yml`** — `caddy` (published 80/443), `api` (`build: .`, tagged
  `${IMAGE}`, no host port), `redis` (no host port). The dev `docker-compose.yml` is untouched.
- **`caddy/Caddyfile`** — `api.example.com` and the status site, auto TLS. The `dynamic a` upstream (via Docker
  DNS `127.0.0.11`) re-resolves `api` per request, so after a recreate Caddy finds the new
  container's IP instead of caching the dead one. No retry directives — see "What zero
  downtime means here" for why they do not help with a single container.
- **`deploy/deploy.sh`** — the poll+build+deploy script (run from the checkout by the timer).
  It **re-execs itself** when the pull changed it: bash reads a script from the handle it
  opened at startup, so without that, a change to this file lands only on the next poll -
  and a step added here does nothing on the deploy that introduced it (section 15).
  After `up -d` it also **validates and reloads Caddy**, because `up -d` does not recreate a
  container whose only change is its mounted config. It runs `caddy validate` then `reload`
  after `up -d`, because a Caddyfile change ships as a config edit that recreates nothing.
  It **reports on itself to Discord** - green on a deploy, red on any non-zero exit via an
  `EXIT` trap - rather than leaving a broken pipeline to be noticed (section 12).
- **`deploy/rovertools-deploy.{service,timer}`** — the systemd units that poll ~every 90s.
- **`netdata/conf/**`** — Netdata config, mirroring `/etc/netdata/`: agent settings and the
  noise trim in `netdata.conf`, collector jobs in `go.d/`, notifications in
  `health_alarm_notify.conf`. Installed into the `netdataconfig` volume by `deploy.sh`, not
  bind-mounted (section 12), so it ships from git regardless.

**Why the Redis flags** (`--save "" --appendonly no --requirepass --maxmemory 256mb
--maxmemory-policy volatile-ttl`): it holds only pub/sub + presence, so persistence off (an
RDB would only burn IO writing data nothing reads after a restart); `requirepass` even with
no published port, because any process on the compose network can reach it; `volatile-ttl`
eviction is safe *only because* the backend re-`SET`s presence keys on every heartbeat pong
(backend commit `4f841d0`), so an evicted key self-heals. `REDIS_URL` in `.env` is
`redis://:<password>@redis:6379/0`.

### One-time box wiring (still to do)

Runs as the login user (`ubuntu`), which is already in the `docker` group, in `~/app`. No
separate service account: polling means nothing logs in to deploy, so a dedicated user buys
little here and adds friction. The box reaches **out** to GitHub with a read-only token over
HTTPS (section 5.6); nothing reaches in.

```bash
# 1. Create a read-only fine-grained token (section 5.6) and keep it handy as $TOKEN.
#    Owner NotRover, repo RoverTools-Smart-Clipboard-App-Backend, Contents: Read-only.

# 2. Clone the repo into ~/app over HTTPS with that token, then lock down .git/config:
git clone "https://x-access-token:${TOKEN}@github.com/NotRover/RoverTools-Smart-Clipboard-App-Backend.git" ~/app
chmod 600 ~/app/.git/config
#   The token is now embedded in the origin remote URL; deploy.sh's git fetch uses it as-is.
#   To rotate: git -C ~/app remote set-url origin "https://x-access-token:<new>@github.com/NotRover/RoverTools-Smart-Clipboard-App-Backend.git"

# 3. Secrets — .env lives INSIDE the checkout (git-ignored, so `git reset --hard` keeps it):
install -m 600 /dev/null ~/app/.env    # then fill it (see Secrets below)

# 4. (Nothing to install for rollovers.) The ~1-3s 502 window per deploy is accepted; see
#    "What zero downtime means here" for what was tried and rejected. Adding real overlap
#    later is a deliberate change to deploy.sh, not a plugin drop-in.

# 5. Install the systemd timer:
sudo cp ~/app/deploy/rovertools-deploy.service /etc/systemd/system/
sudo cp ~/app/deploy/rovertools-deploy.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rovertools-deploy.timer

# First deploy (brings up caddy + redis too) — kick it once instead of waiting for the poll:
sudo systemctl start rovertools-deploy.service
journalctl -u rovertools-deploy.service -f     # watch it build and come up
```

The systemd unit hardcodes `User=ubuntu` and `/home/ubuntu/app`; edit both if you clone
elsewhere. If the box still carries the retired push-deploy `deploy` user, remove it now:
`sudo userdel -r deploy`, drop `deploy` from sshd's `AllowUsers` (section 4), and
`sudo rm -rf /opt/rovertools`.

### Secrets

Never in the image, never in git. They live in `~/app/.env` (`/home/ubuntu/app/.env`), mode
600, read by compose `env_file`. The set the app expects (from `.env.example`):

- `DATABASE_URL` — Supabase pooler URI (section 7a).
- `REDIS_URL` = `redis://:<password>@redis:6379/0`; `REDIS_PASSWORD` also set for the redis
  service's `--requirepass`.
- `SUPABASE_URL`, `SUPABASE_JWT_AUDIENCE`, (`SUPABASE_JWT_SECRET` only for a pre-2025-10
  project), `SUPABASE_SERVICE_ROLE_KEY`.
- `S3_ENDPOINT_URL`, `S3_BUCKET`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`.
- `BREVO_API_KEY`, `EMAIL_FROM` (or the SMTP set).
- `ADMIN_API_KEY`.
- `APP_ENV=production`, `DOCS_ENABLED=false`.
- `METRICS_AUTH_USER`, `METRICS_AUTH_HASH` — read by **Caddy**, never by the app. Basic auth
  for the Netdata dashboard (section 12). Caddy refuses to start without the hash, on purpose.
- **`PUBLIC_BASE_URL=https://api.example.com`** — the base of every user-facing link
  (invites, password-reset redirect). `APP_CORS_ORIGINS` already lists the Tauri client
  origins and does not change.

### Rollback

Every build is tagged `rovertools-api:<short-sha>` and the last few are kept on the box, so a
rollback is redeploying an earlier one — same recreate, backwards (Caddy smooths it as usual):

```bash
cd ~/app
IMAGE=rovertools-api:<old-sha> docker compose -f docker-compose.prod.yml up -d api
# or, if that image was already pruned, check out the commit and rebuild:
#   git checkout <old-sha> && deploy/deploy.sh --force   (then `git checkout main` when done)
```

Note the poll will try to move you back to `origin/main` on its next tick — for a lasting
rollback, revert the commit on `main` (or stop the timer while you investigate:
`sudo systemctl stop rovertools-deploy.timer`). If a rollback also needs a schema revert,
that is a separate, deliberate `migrate.yml` downgrade — the real protection is expand/contract
(section 12).

---

## 10. Verify the deployment

```bash
curl -i https://api.example.com/internal/healthz
```

Check, in order:

- **`/internal/healthz`** returns 200 with Postgres and Redis both healthy; TLS cert valid.
- An **`X-API-Version`** header is present on every response.
- **`/api/docs`** returns 404 unless `DOCS_ENABLED=true`. Leave it off in production; the
  schema maps the admin surface as well as the client one.
- **Auth works end to end** — sign in via Supabase, then call an authenticated route with
  `Authorization: Bearer <jwt>` and `X-Device-Id: <id>`. A 401 here almost always means a
  JWT config mismatch (section 13).
- **WebSocket connects and stays open**: `wss://api.example.com/ws?token=<jwt>&device_id=<id>`.
- If `ADMIN_API_KEY` is set, `/internal/metrics` with `X-Admin-Key` returns 200 (503 means
  the key is unset).

Also run the drills. **Deploy** — push a trivial change (or `deploy/deploy.sh --force`) and
watch the swap; measure the gap rather than assume it:

```bash
cd ~/app && ./deploy/deploy.sh --force >/tmp/deploy.log 2>&1 &
while kill -0 $! 2>/dev/null; do
  curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" https://api.example.com/internal/healthz
  sleep 0.3
done
```

Expect 200s with **two 502s** at the recreate (one immediate, one ~3s dial timeout) — that is
the known, accepted window, not a regression. Then **rollback** (redeploy the previous SHA) and
**reboot** (`sudo reboot`, confirm the stack returns on its own via `restart: unless-stopped`).

---

## 11. Point the desktop app at it

The client holds all key material and does all encryption; the server only ever sees
ciphertext. It needs the API base URL and Supabase credentials for sign-in. The client
authenticates against Supabase directly and forwards the access token as an opaque string —
it never inspects the JWT, so the asymmetric-key change in section 7a needs no client change.

**`DEFAULT_SERVER_URL` in `orange-copy-paste-clipboard-app-rust/src-tauri/src/sync/config.rs`
now points at `https://api.example.com`** — repointed off Render in source, but baked in
at build time, so it reaches users only in a **client release** (none shipped yet). An existing
install can be repointed sooner by editing `sync_server_url` in `settings.json`. The old Render
service is retired, so there is no endpoint running in parallel during the switch — anyone who
has not updated is offline until they do. Make sure `APP_CORS_ORIGINS` includes the app's origin
(`tauri://localhost` by default).

---

## 12. Ongoing operations

**Monitoring (Netdata).** Runs as the `netdata` service in the prod stack, published by Caddy
at `https://status.example.com` behind basic auth, with its metrics database in the
`netdatalib` volume. It replaced Uptime Kuma, which answered "is it up" and nothing else
(section 15). Out of the box it charts CPU, memory, disk space and IO, network, pressure
stall, systemd unit states, and per-container CPU/memory/IO for every service in the stack -
at one-second resolution, with alarms already defined for the things that matter.

**Basic auth is not optional.** The agent dashboard has no login of its own and reports
processes, listening ports, disk layout and container internals. Published bare, it is a free
reconnaissance page for the box. Generate the hash on the box and put it in `.env` - nobody
needs to see the password but you:

```bash
docker run --rm -it caddy:2-alpine caddy hash-password
```

It prompts, so the password never reaches your shell history. This runs a throwaway
container rather than `exec`-ing into the running one on purpose: `METRICS_AUTH_HASH` is
required by the compose file, so while it is unset **every** compose command fails - there
would be no `caddy` container to exec into. Paste the whole `$2a$...` string:

```bash
cd ~/app
printf 'METRICS_AUTH_USER=admin
METRICS_AUTH_HASH=<paste the hash, every $ doubled>
' >> .env
./deploy/deploy.sh --force
```

**Double every `$` in the hash.** Compose interpolates values it reads out of `.env`, so a
bcrypt hash pasted raw is read as three variable references and arrives **blank**:

```
METRICS_AUTH_HASH=$2a$14$K3q...      # WRONG - $2a, $14, $K3q... all expand to nothing
METRICS_AUTH_HASH=$$2a$$14$$K3q...   # right
```

A blank hash fails closed: Caddy answers 401 to everyone, including you, which looks exactly
like working auth from the outside. Prove the value arrived instead of assuming it:

```bash
cd ~/app
docker compose -f docker-compose.prod.yml exec caddy printenv METRICS_AUTH_HASH
```

That must print the whole `$2a$14$...` string with single `$`. If it prints nothing, the
escaping is wrong. Then prove it end to end - **both** lines, because a 401 on its own is
also what a blank hash produces:

```bash
curl -s -o /dev/null -w 'no-auth %{http_code} (expect 401)
' https://status.example.com
curl -s -o /dev/null -u admin -w 'with-auth %{http_code} (expect 200)
' https://status.example.com
``` Caddy also **refuses to start** if `METRICS_AUTH_HASH` is unset entirely,
which is deliberate: a missing password should be a site that does not come up, not a site
that comes up unprotected.

**What it watches beyond the machine.** Two additions to the stock config, both in the repo:

| Check | Where | Why it is not the default |
|---|---|---|
| `api_direct` -> `http://api:8000/internal/healthz` | `netdata/conf/go.d/httpcheck.conf` | Matches the **body** for `"status":"ok"` |
| `api_public` -> `https://api.example.com/internal/healthz` | same | Same match, through Caddy and TLS |

Neither is bind-mounted. Netdata's entrypoint copies stock config into `/etc/netdata` on
every start, so a read-only mount anywhere under that path makes the copy fail and the
container crash-loop. `deploy.sh` installs these files into the `netdataconfig` volume
instead and restarts Netdata only when their content changed - so they ship from git with
the code, without a step you have to remember.

The body match is the whole point. `/internal/healthz` returns **200 even when degraded** - a
dead Postgres or Redis shows only in the body (`src/admin/router.py`) - so a status-code check
would stay green straight through a database outage. Two jobs because the pair localises a
fault: public failing while direct passes means Caddy, TLS or DNS; both failing means the app,
Postgres or Redis.

**The dashboard is trimmed, on purpose.** Netdata's defaults collect everything a machine
*could* have, which on a small VPS means the handful of charts that matter are buried under
hardware we do not own and kernel counters nobody will act on. `netdata/conf/netdata.conf`
switches those off. The single biggest cut by far is **`apps = no`** - see below. Per-systemd-
service cgroup charts are also gone (22 units, 7 charts each) via
`cgroups to match as systemd services = !*`, which is what governs them in Netdata v2 after
the old `enable systemd services` switch was removed. Note those were cgroup resource charts,
not unit state - unit state is a separate plugin that does not run here at all (see the gaps
below). Anomaly detection is off too: a model
per dimension costs real CPU and memory here, and produces a second thing to interpret rather
than an answer.

The second is `netdata monitoring = no`: the agent's charts about *itself* - dbengine
compression ratio, database pages, worker thread timings, query latency. That is the whole
"Netdata Monitoring" menu, and it answers questions about the monitoring tool rather than
about the box. Also gone: pressure stall, interrupts and softirqs, deep TCP kernel counters
(out-of-order segments, SYN cookies, ECN) and conntrack, IPv6/SCTP/NFS/IPVS stacks, statsd,
eBPF, ZFS, Btrfs, software RAID, batteries, ECC, Infiniband, NUMA, entropy and SysV IPC.
Network interfaces are filtered to the real uplink, because Docker gives every compose
network a bridge and every container a veth, each of which otherwise becomes a menu entry
named after a hash; disks drop loopback, ramdisk and device-mapper entries.

The big one is `apps = no`. apps.plugin charts every application, user and user group
separately - 644 + 168 + 154 + 46 charts here, roughly 90% of what survived the other cuts,
answering nothing the per-container charts do not. Per-process detail is what `htop` and
`docker stats` are for, and both are already on the box.

What is deliberately kept, because it is the list you would want during an incident: CPU,
RAM and swap, disk space and IO, network throughput, per-container CPU/memory/IO for all
five services, and the two API health checks. Unit state is
narrowed to the units worth alarming on in `netdata/conf/go.d/systemdunits.conf` rather than
all of them.

The agent does not fail on a key it does not recognise, but it usually **says so** - the
config it serves back at `/netdata.conf` marks an unknown key `found in the config file, but
is not used`, and annotates a renamed one with `migrated from`. **`[plugins]` is the
exception, and it is a trap:** that section takes an arbitrary plugin name as a key, so a
misspelled plugin is accepted in silence and simply does nothing. Never write a plugin name
from memory - read it off the `plugin=` field of the charts you want gone:

```bash
cd ~/app
docker compose -f docker-compose.prod.yml exec -T netdata   curl -s 'localhost:19999/api/v1/charts' | python3 -c "import json,sys; d=json.load(sys.stdin)['charts']; s={}; [s.__setitem__((c.get('plugin'),c.get('module')), s.get((c.get('plugin'),c.get('module')),0)+1) for c in d.values()]; [print('%4d  %-22s %s' % (n,p,m)) for (p,m),n in sorted(s.items(), key=lambda x:-x[1])]"
```

Then check both the count and the served config:

```bash
cd ~/app
docker compose -f docker-compose.prod.yml exec -T netdata   curl -s 'localhost:19999/api/v1/charts' | grep -o '"id":"' | wc -l
```

**The deploy reports on itself.** A pipeline that stops working is silent by nature: the
timer fires, the script fails early, the old containers keep serving, and nothing looks
wrong until someone notices a merged commit never shipped. That happened twice here
(section 15). So `deploy.sh` posts to the same Discord channel as the alarms, and only when
there is something to say - the ~90s no-op polls are silent:

| When | Message |
|------|---------|
| A commit deployed | Green **Deployed `rovertools-api:<sha>`**, with the commit subjects that shipped (up to 8, then a count), the commit range, files changed, wall-clock duration, and whether Caddy reloaded and how many Netdata config files were installed |
| The range shipped a migration | The same, **amber**, with a `MIGRATIONS` field naming how many revision files arrived. A deploy never runs Alembic, so the database is now behind the code and the symptom is a live route 500ing on a missing relation |
| Any non-zero exit | Red **Deploy FAILED on `<host>`**, naming the **stage** it died in (`git fetch`, `docker build`, `container rollout`, `caddy reload`, `netdata config`), the exit code, the commit, and the `journalctl` line to run |

The embed JSON is built by `python3` reading environment variables, not by pasting strings
together in shell. Commit subjects contain quotes, backslashes and non-ASCII; a hand-rolled
shell escaper gets one of those wrong eventually, and the failure mode is a webhook silently
rejecting the post. If `python3` is ever missing the deploy says so and carries on rather
than dying inside its own error handler.

Two values are carried across the self-re-exec (section 5.4) in the environment: the
**pre-pull commit** and the **start time**. Without the first, the re-exec'd process compares
HEAD against itself and reports an empty commit list - which is exactly why the first
notifications said nothing but the image tag.

The failure path is an `EXIT` trap, so it covers every way the script can die - a failed
`git fetch`, a broken build, a container that will not come up - not just the errors someone
thought to handle. It does not fire on the self-re-exec (section 5.4), because `exec`
replaces the process image without running traps.

This is deliberately independent of Netdata. `rovertools-deploy.service` is a oneshot that is
inactive between runs, and inferring "the pipeline is healthy" from a unit that is *supposed*
to be idle most of the time is exactly the kind of indirect guarantee that failed us twice.
The thing doing the work reports on the work. The webhook is read from `.env` and a failed
post is non-fatal - a broken notifier must never break a deploy.

**Notifications.** Alarms reach Discord through a **custom sender** defined in
`netdata/conf/health_alarm_notify.conf` in the repo. Two decisions worth knowing:

- **Not email.** OVH filters outbound SMTP (the same reason the app sends through Brevo's
  HTTPS API, section 13), so `SEND_EMAIL="NO"`. Any mail-based alert fails silently.
- **Not the stock Discord sender.** It works, but its message is a wall of italic prose.
  `SEND_DISCORD="NO"` and `SEND_CUSTOM="YES"` instead; the custom sender posts a colour-coded
  embed - red critical, amber warning, green recovered - with the value, chart and previous
  state as separate fields. Severity is carried by the embed colour, so the text stays plain
  ASCII and reads on a phone. Only one of the two senders may be enabled, or every alarm
  arrives twice.

The config is a **minimal override**: `alarm-notify.sh` sources the stock file first and this
one second, so anything not named here keeps its stock behaviour.

**The webhook is the only part not in git.** Create it in Discord (Server Settings ->
Integrations -> Webhooks -> pick a channel -> Copy Webhook URL), then:

```bash
cd ~/app
echo 'ALERT_DISCORD_WEBHOOK=<paste the webhook URL>' >> .env
./deploy/deploy.sh --force
```

It is passed to the container by `docker-compose.prod.yml` and read by the sender. The name
matters: the stock notify config assigns `DISCORD_WEBHOOK_URL=""` before ours is sourced, so
reusing that name would shadow the value with an empty string.

**Test it, do not assume it.** Netdata ships a test path that fires all three states:

```bash
cd ~/app
docker compose -f docker-compose.prod.yml exec -T netdata   bash -c '/usr/libexec/netdata/plugins.d/alarm-notify.sh test'
```

Three messages should arrive - warning, critical, recovered. If none do, that command says
why; read it rather than inferring success from silence.

**What actually reaches you, and what does not.** The whole point of the setup, in one table:

| You get pinged when | From | Colour |
|---|---|---|
| A commit deployed | `deploy.sh` | Green |
| A deploy failed, for any reason | `deploy.sh` exit trap | Red |
| The API stops answering, or answers 200 with a degraded body | Netdata `httpcheck` | Red |
| Public URL fails while the container is fine (Caddy, TLS, DNS) | `api_public` fails, `api_direct` passes | Red |
| A container is killed, restarts, or eats CPU/memory | Netdata cgroup alarms | Amber then red |
| Disk fills, RAM or swap runs out, load spikes | Netdata system alarms | Amber then red |
| Any of the above recovers | Netdata | Green |

Nothing pings you for a routine ~90s poll that found no new commit, and nothing pings you
for the categories trimmed above. Three gaps remain, all known:

- **The box being down or off the network.** Everything above runs on it. The external check
  below is the only thing that catches this.
- **Anything inside Supabase**, which is not this box at all.
- **systemd unit state.** `fail2ban` or `nftables` dying is silent. The `systemd-units`
  plugin is its own plugin (not a go.d module, whatever older notes say) and it produces
  nothing in this container, because it talks to systemd over D-Bus and `/run/systemd` is
  not mounted. Closing this means mounting the host's systemd socket *and* filtering to a
  few units, or every unit on the box lands back on the dashboard. Not done; the units that
  would take the service down with them (`docker`, the API container) are already covered
  by the container and health-check alarms.

**Rotate the webhook if it has been pasted anywhere shared.** Anyone holding the URL can post
into that channel. Regenerating is one click in Discord, then replace the line in `.env` and
re-run the deploy. The blast radius is spam in one channel, not access to the box - but it is
free to fix.

**What this still cannot tell you.** Netdata runs on the box it watches, so if the VPS is down
or off the network, the dashboard is down with it and no alert is sent. A **free external
check** (UptimeRobot, Better Stack) hitting `https://api.example.com/internal/healthz`
with a keyword match on `"status":"ok"` is the only thing that catches a whole-box outage. Run
one alongside this; it is the one piece that cannot live on the box.

**Retention and footprint.** Netdata's default database tiers keep roughly a day of
per-second data and months of downsampled history, sized to what the `netdatalib` volume can
take. On this box (3.7 GiB RAM, 38 GB disk) that is comfortable, but it is the largest thing
in the stack by memory. The collector trim above already removed most of the cost; if it ever
crowds the API again, cut retention in `netdata/conf/netdata.conf` and deploy - editing it in
the container instead (`./edit-config`) puts the box out of step with git, which is the exact
drift `netdata/conf/**` exists to prevent.

**Netdata Cloud stays unclaimed.** `DISABLE_TELEMETRY=1` is set and no claim token is
configured, so the agent talks to nobody. Claiming it would put the box's metrics on someone
else's dashboard; that is a decision to make deliberately, not to drift into.

**Inspecting logs.** Everything lands in the host's systemd journal, which is persistent and
survives the container swaps a deploy makes. Two surfaces: the deploy runner and the app
containers. The deploy runner tells you whether a poll picked up a commit and whether the
build/rollout succeeded:

```bash
journalctl -u rovertools-deploy.service -f              # live, follow (Ctrl-C to stop)
journalctl -u rovertools-deploy.service -n 100 --no-pager   # last run's output
systemctl status rovertools-deploy.timer               # poll active? last / next fire
```

The containers log to the journal via the `journald` driver, tagged per service
(`docker-compose.prod.yml`). Read them by tag — the tag is stable across rollouts, so you see
old and new containers under one name:

```bash
journalctl -t rovertools-api -f            # API, live
journalctl -t rovertools-api -n 200 --no-pager
journalctl -t rovertools-api --since '1h'  # bound the window (or --since 10m)
journalctl -t rovertools-caddy             # TLS / proxy
journalctl -t rovertools-redis
```

Because it is the journal, not ephemeral container output, `--since`/`--until` reach back past
the current container's lifetime. To hand someone a plain file, redirect any of the above:
`journalctl -t rovertools-api --since today > api-$(date +%F).log`. Still-useful Docker views
for state (not history): `docker compose -f docker-compose.prod.yml ps` (health, restarts) and
`docker stats --no-stream` (per-container CPU/memory).

The journal is capped, not infinite: persistent storage and a 500 MB / one-month ceiling are
set in section 5.8, so old lines age out rather than filling the disk.

**Schema changes.** Author the Alembic revision and review it, then apply it yourself
(section 8) *before* the deploy that needs it — a green deploy is not evidence anything
migrated. Because a rolling deploy runs **old and new code against one DB at the same time**,
never ship a breaking migration in one shot: additive migration (deliberate) -> deploy code
-> later, contractive migration (deliberate). Expand/contract.

**Supabase key rotation.** Rotating an asymmetric signing key needs no action — the JWKS is
re-fetched (cached ~5 minutes). Rotating a *legacy* HS256 secret means updating
`SUPABASE_JWT_SECRET` and redeploying, which invalidates live sessions.

**Scaling.** Increase `api` replica count freely; Redis pub/sub handles cross-replica
fan-out and the pool bounds (24 request / 2 listener per replica, backend commit `4f841d0`)
stay well under a self-hosted Redis's limits. Redis holds only ephemeral presence and
pub/sub traffic, so it stays small.

**Patching and reboots.** unattended-upgrades patches nightly and reboots at 04:30 UTC when
a patch needs it (section 4); that briefly drops WebSockets and clients reconnect. Docker
itself is patched the same way (Ubuntu packages).

**Costs.** Fixed VPS cost, plus external tiers. **Supabase free** gives 500 MB Postgres +
5 GB egress and **pauses after ~1 week idle**; text entries are tiny, so the DB is rarely
the wall, and the meaningful first bill is **Supabase Pro (~$25/mo)** for always-on plus
headroom. **R2 free** is 10 GB storage with **zero egress**, and ~$0.015/GB-mo beyond it —
binary blobs are the real storage cost; at the 50 MB default quota the 10 GB free pool
covers ~200 users before R2 costs anything. Redis is in-container and free. Per-user
storage is capped by `profiles.blob_bytes_quota` (default 50 MB, set via
`DEFAULT_BLOB_QUOTA_BYTES`, overridable per user via the admin quota endpoint) and a 5 MB
per-entry hard cap.

**Supabase and R2 stay external.** The migration onto the VPS did not touch them; the
`DATABASE_URL` secret on the migrate workflow keeps working untouched.

---

## 13. Troubleshooting

**Every authenticated request returns 401.** Almost always a JWT mismatch. Decode the token
(jwt.io) and read the `alg` header:

- `alg: ES256`/`RS256` -> `SUPABASE_URL` must be set and correct; the backend derives
  `<SUPABASE_URL>/auth/v1/.well-known/jwks.json` from it.
- `alg: HS256` -> `SUPABASE_JWT_SECRET` must be set and match the project.

Also confirm the `aud` claim is `authenticated` (`SUPABASE_JWT_AUDIENCE`).

**500 "SUPABASE_URL is not configured"** — an asymmetric token arrived but no project URL is
set. **500 "...SUPABASE_JWT_SECRET is not configured"** — an HS256 token arrived on a
deployment configured only for asymmetric keys.

**`OSError: [Errno 101] Network is unreachable` on every DB call.** `DATABASE_URL` points at
the direct `db.<ref>.supabase.co` host (IPv6-only) and v6 routing is failing. The VPS has
IPv6, but switch `DATABASE_URL` to the Supavisor pooler (section 7a) to remove the
dependency. Symptom: the service starts fine, logs `maintenance loop error; retrying` in a
loop, and fails its health check.

**`prepared statement "__asyncpg_..." does not exist`.** You are on the transaction pooler
(port 6543) with statement caching on. `database.py` disables it automatically for `:6543`
URLs — if you see this, the port is not literally in the URL, so switch to session mode
(5432).

**Health check fails.** `/internal/healthz` touches Postgres and Redis. Check `DATABASE_URL`
(pooler host? asyncpg driver? password URL-encoded?) and that the `redis` container is up
(`docker compose -f docker-compose.prod.yml ps`).

**WebSocket connects then drops.** A single drop right after a deploy or the 04:30 reboot is
expected — clients reconnect. Persistent drops: confirm the token is passed as the `token`
query parameter (browsers and Tauri cannot set headers on a WebSocket handshake) and that
Caddy is proxying `/ws` (it does by default).

**Invite emails never arrive.** Delivery is best-effort: `send_sharing_invite` logs the
failure and swallows it so the inviter's request still succeeds, which means a broken mail
config looks exactly like a working one from the client. Ask the deployment what it thinks
it is doing:

```bash
curl -s -H "X-Admin-Key: $ADMIN_API_KEY" <base>/internal/v1/admin/email
curl -s -X POST -H "X-Admin-Key: $ADMIN_API_KEY" -H 'Content-Type: application/json' -d '{"to":"you@example.com"}' <base>/internal/v1/admin/email/test
```

The first reports the provider and whether its credentials are present (no secret echoed);
the second sends one message and returns the actual error. Two causes cover nearly every
case:

- `provider: brevo, brevo_api_key_set: false` — `BREVO_API_KEY` is missing from the `.env`.
  Sends raise before touching the network.
- `provider: smtp` and nothing arrives — OVH filters outbound SMTP (port 25 is blocked, and
  submission ports can be too). Prefer `EMAIL_PROVIDER=brevo`, which sends over HTTPS and is
  not affected. Account emails (verification, password reset) come from Supabase and are
  unaffected either way.

**Account mail looks nothing like the invite.** Supabase renders its own templates from its
dashboard, and out of the box they are its stock ones. `docs/supabase-email/` holds copies
built from this repo's mail shell — paste them into Authentication -> Emails -> Templates,
subjects included, and re-run `uv run python scripts/render_supabase_emails.py` after any
change to the shell. The built-in Supabase mailer is rate limited to a couple of messages an
hour, so configuring custom SMTP is what makes signup and reset mail dependable.

**Blob upload fails, everything else works.** R2 misconfiguration. Verify the bucket exists,
`AWS_REGION=auto`, and the endpoint is the account-level R2 URL. Presigned PUTs expire in 5
minutes and GETs in 1 hour, so a badly skewed client clock also breaks uploads.

**A Caddyfile change does not take effect, and `caddy reload` says "config is unchanged".**
The container is reading a stale copy. A *single-file* bind mount binds the inode, and `git
pull` replaces the file rather than editing it in place, so the container keeps the inode it
started with - `validate` and `reload` inside the container then both operate on the old
file. Fixed by mounting the `caddy/` **directory** instead (`./caddy:/etc/caddy:ro`), which
resolves the path fresh, plus a reload step in `deploy.sh`. If you meet this on a container
predating that fix, `docker compose -f docker-compose.prod.yml up -d --force-recreate caddy`.
Symptom to recognise: the file on disk clearly has your change, `caddy validate` passes, and
the reload logs `"config is unchanged"`.

**Two builds of one commit differ.** Fixed once the `Dockerfile` builds `--frozen` from
`uv.lock` (section 9). Until then, the image installs with `uv pip install -e .` resolved
from `pyproject.toml`, so `uv.lock` does not pin the deployed image.

---

## 14. Decisions — settled and open

**Settled 2026-08-24:**

- **Host** — self-hosted OVH VPS-1, replacing Render. Fixed monthly cost, full control, and
  Redis co-located so it can bind to an internal network with no tunnel or TLS.
- **Ingress/domain** — a free FreeDNS name, `api.example.com` -> `203.0.113.10`,
  Caddy owning TLS via Let's Encrypt. Chosen over: an owned domain (~$10/yr — swap later by
  changing one Caddyfile hostname + `PUBLIC_BASE_URL`), and a Cloudflare Tunnel (zero
  inbound ports but another hop in the WebSocket path and a daemon to keep up). Temp name
  first to bring the pipeline up for free.
- **Deploy stack** — Docker Compose + Caddy, on a systemd poll. The draw is tagged images and
  one-step rollback.
- **Deploy rollover** — plain `docker compose up -d` recreate, **accepting a ~1-3s 502 window
  per deploy**, over an overlap tool. `docker-rollout` was declined on trust (a third-party
  script with Docker/root access); Swarm as cluster-weight on one host; and a Caddy
  `lb_try_duration` retry was tried and measured not to help (one container = an empty pool,
  nothing to fail over to). Infrequent deploys and retrying clients make the blip cheap. See
  "What zero downtime means here".
- **Build + delivery** — **build on the box, polled from git**, over GitHub Actions +
  GHCR. A private repo's Actions minutes and (worse) its 500 MB Packages storage both cost
  money as builds pile up; building on the VPS we already pay for spends nothing on GitHub,
  and a `git`-poll needs no inbound endpoint, no webhook secret, and no CI credentials on the
  box. Cost was the deciding factor (the user's call); the trade is a ~90s deploy latency and
  the VPS doing the build. Rejected alternatives: GitHub Actions + GHCR push (the cost we are
  avoiding); a self-hosted Actions runner (unlimited minutes but still a runner daemon + the
  Actions dependency); a webhook receiver (instant, but an inbound endpoint + HMAC to secure).
- **Deploy identity** — runs as the `ubuntu` login user (already in `docker`) with an outbound
  read-only git key, over both an inbound forced-command CI key and a separate no-SSH service
  account. Polling removed any inbound path, so a dedicated user bought little; this is strictly
  less surface and less friction.
- **Redis** — a compose service, not a host package, so it is versioned and torn
  down/rebuilt with the stack.

**Open / to do:**

- **Client cutover release (section 11)** — `DEFAULT_SERVER_URL` is repointed at
  `https://api.example.com` in source, but it reaches users only in a client release,
  which has not shipped yet.
- Retire the old push-deploy `deploy` user if the box still carries it: `sudo userdel -r
  deploy`, drop `deploy` from `AllowUsers`, `sudo rm -rf /opt/rovertools`.
- Move off the temp FreeDNS name to a permanent domain when ready (Caddyfile + `PUBLIC_BASE_URL`).
- **Add a free external uptime check** on the public healthz with a `"status":"ok"` keyword
  match. Nothing on the box can report the box being down.

---

## 15. History

- **2026-08-24 — box provisioned and hardened.** SSH locked to key-only, root off,
  `AllowUsers ubuntu`; ufw up (deny-in, 22/80/443); fail2ban + unattended-upgrades with
  nightly auto-reboot.
- **2026-08-24 — SSH lockout, recovered.** Disabling password auth locked out new sessions
  because `ubuntu`'s `authorized_keys` was **empty** — every login until then had been by
  password, and the intended key had never been installed. Recovered by appending the public
  key from the still-open session. **Lesson, now the rule in sections 3 and 5:** install and
  test the key *before* disabling password auth, and never close the working session until a
  new one proves the change.
- **2026-08-24 — passphrase vs account password, cleared up.** Changing the `ubuntu` account
  password with `passwd` looked like it broke SSH; it had not. The workstation prompt wants
  the *key passphrase*. Both are documented in section 2. KVM console login with the `ubuntu`
  password confirmed working.
- **2026-08-24 — Docker + domain.** Installed Docker (Ubuntu packages) and pointed
  `api.example.com` (FreeDNS) at the box. A `deploy` service account was created here
  under the push-deploy design, then dropped when the deploy moved to run as `ubuntu`; remove
  it if the box still has it. No application containers running yet.
- **2026-08-24 — docs consolidated.** The Render/Supabase/R2 runbook and the two parent-repo
  VPS docs (`VPS-RUNBOOK.md`, `DEPLOY-VPS.md`) folded into this single file; the Render path
  was retired.
- **2026-08-24 — deploy design settled on build-on-box + git poll.** First drafted as GitHub
  Actions + GHCR push with a forced-command CI key; changed to building on the VPS and polling
  `origin/main` from a systemd timer, to spend nothing on Actions minutes or Packages storage
  (section 14). Wrote the stack on `deploy/vps-docker` — `Dockerfile` (frozen/non-root/
  healthcheck), `docker-compose.prod.yml` (local build), `Caddyfile`, `.dockerignore`,
  `deploy/deploy.sh`, `deploy/rovertools-deploy.{service,timer}`; removed `render.yaml`;
  repointed `public_base_url` and the README/ARCHITECTURE notes off Render. Not yet deployed.
- **2026-08-24 — first live deploy.** Ran as `ubuntu` in `~/app`: read-only deploy key, clone,
  `.env`, systemd poll timer, first `docker compose up`. Two fixes surfaced on the real box and
  could not have on the Windows workstation: the `Caddyfile` needed the block form of
  `dynamic a` (the inline `resolvers` failed to parse and crash-looped caddy), and container
  logs moved to the persistent `journald` driver so they survive a rollout (section 5.8, 12).
  `https://api.example.com/internal/healthz` returns 200 with `db` and `redis` ok, valid
  TLS, `via: 1.1 Caddy`. Backend is live; client cutover still pending (section 11).
- **2026-08-24 — deploy rollover: accept the blip.** Weighed docker-rollout (declined:
  third-party script with Docker/root access) and Swarm (declined: cluster-weight on one host,
  and `stack deploy` cannot build). Tried a Caddy `lb_try_duration` retry as the free middle
  option; **measured on the box, it does not work** — config confirmed live via `caddy adapt`
  (`try_duration: 10000000000`) and the swap still produced the same two 502s, because retry
  needs another healthy host and one container means an empty pool. Removed the retry rather
  than leave misleading config, and settled on plain `docker compose up -d` with a known
  ~1-3s 502 window per deploy. Also fixed `deploy.sh`: its `docker rollout` guard exited 0
  with no plugin installed, so the branch ran and broke the deploy. Reproduce the measurement
  with a watch-curl during `deploy.sh --force`.
- **2026-08-24 — monitoring: Uptime Kuma on the box.** Added as the `kuma` compose service
  behind Caddy on `status.example.com`, with no Docker socket mounted (container monitors
  would mean root-equivalent access for a web-facing service, the docker-rollout objection
  again). Health monitors must be **keyword** checks on `"status":"ok"`, because
  `/internal/healthz` answers 200 while degraded and a status-code check would stay green
  through a Postgres outage. Email notifiers are unusable (OVH filters SMTP) — use an HTTPS
  notifier. Kuma cannot report a whole-box outage since it shares the box; pair it with a free
  external check (section 12).
- **2026-08-24 — the Caddyfile was never actually shipping.** Adding Kuma surfaced it: the new
  site block was on disk and on the right commit, yet `caddy validate` passed and `caddy
  reload` logged `"config is unchanged"`. `docker-compose.prod.yml` bind-mounted the *single
  file* `./Caddyfile`, which binds an inode; `git pull` replaces the file, so the running
  container kept reading the copy it started with, and every Caddyfile change since the last
  container recreate had been silently inert (applying only at the next reboot). Fixed by
  moving the config to `caddy/Caddyfile` and mounting the **directory**, and by adding a
  `caddy reload` step to `deploy.sh` so config changes ship with the code as section 9 always
  claimed they did.
- **2026-08-24 — closing the class, not the instance.** Three failures this month shared one
  shape: something was broken and nothing said so. The `docker rollout` guard exited 0 without
  the plugin; the Caddyfile went inert behind a single-file bind mount; and during the retry
  experiment the config being measured was a stale one, which is why the measurement was
  confusing before it was conclusive. Two structural changes rather than three patches. **(a)
  No single-file bind mounts** — the only host path left in `docker-compose.prod.yml` is the
  `./caddy` *directory*; every other mount is a named volume, so no container can pin an inode
  that git will replace. **(b) The deploy reports on itself** — `deploy.sh` validates the Caddy
  config before reloading it, and pings a Kuma Push monitor on success and `status=down` from
  an exit trap on failure. Silence is now the alarm: no ping in five minutes means the script
  broke or the timer stopped. The heartbeat is what would have caught the `docker rollout`
  bug, which exited 125 and told nobody.
- **2026-08-25 — Uptime Kuma out, Netdata in.** Kuma answered one question, "is the URL
  responding", and answering it well still left the box itself invisible: no CPU, no memory,
  no disk trend, no per-container usage. Machine health had to be bolted on as a shell script
  pushing numbers into a fake monitor, which is a sign the tool was wrong rather than
  incomplete. Netdata replaces it and the scaffolding around it: removed the `kuma` service and
  `kuma_data` volume, `deploy/host-health.sh` and its timer, and the push-heartbeat plumbing in
  `deploy.sh`. What each of those guaranteed still holds, by a different route - the healthz
  **body** match moved into `netdata/go.d/httpcheck.conf` (a status-code check would still stay
  green through a Postgres outage), and the deploy heartbeat was replaced - first by a
  systemd-units alarm, then, when that proved to rest on a unit that is idle by design, by
  the deploy reporting to Discord directly (see the last entry). Two costs,
  both accepted deliberately: Netdata needs the host read-only (`/proc`, `/sys`, `/`,
  `/var/log`) plus `SYS_PTRACE`, which is *more* box access than Kuma ever had, and its
  dashboard has no login, so Caddy basic auth is now load-bearing rather than a nicety. The
  Docker socket is still not mounted into anything web-facing: container names come from
  `dockerproxy`, allowlisted to `GET /containers`.
- **2026-08-25 — the metrics password arrived blank.** First deploy of the Netdata site
  answered 401 to everyone, which reads as working basic auth and is not: Compose interpolates
  the values it reads out of `.env`, so `METRICS_AUTH_HASH=$2a$14$K3q...` expanded `$2a`,
  `$14` and `$K3q...` as three unset variables and handed Caddy an empty hash. It failed
  closed, so nothing was exposed - but the only way to tell that state from a working one
  from outside is to try logging in. Every `$` must be doubled in `.env`. Verify with
  `docker compose exec caddy printenv METRICS_AUTH_HASH` rather than inferring it from a 401.
- **2026-08-25 — Netdata crash-looped on its own config mount.** The collector config was
  bind-mounted read-only at `/etc/netdata/go.d`, and Netdata's entrypoint copies stock config
  into `/etc/netdata` on every start: `cp: preserving times for '/etc/netdata/go.d':
  Read-only file system`, then exit, then restart, forever. Mounting it read-write is worse -
  Netdata would write dozens of stock files into the git checkout. Netdata's config genuinely
  lives in a volume, so `deploy.sh` now installs `netdata/go.d/*.conf` into it and restarts
  the container only when the content changed. The file still ships from git and still
  arrives by the deploy, which was the point of not bind-mounting a single file in the first
  place - the constraint moved, the guarantee did not.
- **2026-08-25 - measured a memory scare, found no memory problem and no swap.** htop's bar
  looked full and the box was reported to be "reaching memory caps". It was not: 939 MB used
  of 3.7 GB with **2.8 GB available**, all five containers together under 300 MB (api 118,
  netdata 135, caddy 13, redis 6, dockerproxy 4). The full-looking bar was page cache, which
  Linux hands back on demand, and the repeated 106 MB `dockerd` / 141 MB `python` rows were
  threads of one process each, not copies - htop was in thread view. Nothing was tuned:
  trimming Netdata's retention would have blinded the monitoring just installed, in exchange
  for nothing. The real finding was `Swap: 0B` - no runway between healthy and the OOM killer
  picking the API container. Added a 2 GB swap file with `vm.swappiness=10` (section 5.9).
  Read the numbers, not the bar.
- **2026-08-25 - notifications made readable, and reproducible.** The stock Discord sender
  worked but wrote a paragraph of italic prose per alarm. Replaced with a custom sender
  (`SEND_DISCORD="NO"`, `SEND_CUSTOM="YES"`) posting a colour-coded embed: value, chart and
  previous state as fields, severity carried by the embed colour so the text stays plain
  ASCII. The bigger problem was that `health_alarm_notify.conf` lived only in the
  `netdataconfig` volume - typed in by hand, and gone the moment the box is rebuilt. Netdata
  config now lives in `netdata/conf/` in the repo, mirroring `/etc/netdata/`, and `deploy.sh`
  installs the whole tree rather than just `go.d`. The webhook stays out of git, arriving as
  `ALERT_DISCORD_WEBHOOK` from `.env` - named that way because the stock config assigns
  `DISCORD_WEBHOOK_URL=""` before ours is sourced and would otherwise shadow it. A rebuilt box
  now arrives already watching itself, needing only two values in `.env`.
- **2026-08-25 - the deploy script was always one run behind itself.** Bash reads a script
  from the file handle it opened at startup, and `git reset --hard` replaces `deploy.sh` with
  a new inode, so the running copy is always the pre-pull one. Any change to the deploy
  itself took effect on the *next* poll. That is mildly confusing on its own and actively
  dangerous combined with a step that no-ops silently: when the collector-config loop moved
  from `netdata/go.d/*.conf` to `netdata/conf/`, the old loop globbed a path that no longer
  existed, compared empty against empty, found nothing to do and printed nothing - so the
  deploy that shipped the notification config installed none of it, twice, and looked
  successful both times. `deploy.sh` now hashes itself before the pull and re-execs the new
  copy with `--force` when it changed, guarded by `DEPLOY_REEXEC` against looping. The lock
  is kept across the exec by testing `/proc/self/fd/9` rather than assuming.
- **2026-08-25 - deploy alerting rested on an idle unit; the dashboard buried its own
  signal.** Two loose ends from the Kuma removal, closed together. **(a)** The claim that a
  broken pipeline would alarm through Netdata's systemd-units collector was inferred, not
  tested, and it was the wrong shape regardless: `rovertools-deploy.service` is a oneshot
  that is *supposed* to be inactive between polls, so reading health from its state means
  reading a signal that looks identical whether the timer is working or stopped. Replaced
  with the direct thing - `deploy.sh` posts a green embed naming the image on a real deploy
  and a red one from an `EXIT` trap on any non-zero exit, covering every way the script can
  die rather than the errors someone anticipated. A failed post is non-fatal; a broken
  notifier must not break a deploy. **(b)** The dashboard shipped with Netdata's defaults and
  was unreadable for it - about 1900 charts, on top of pressure stall, IPv6/NFS/SCTP stacks,
  ZFS, Btrfs, RAID, batteries, ECC and NUMA on a virtual machine that has none of them. `netdata/conf/netdata.conf` now
  turns those off and ships from git like the rest. Note that unrecognised keys are ignored
  silently, so this is a change that must be verified by counting charts, not by reading the
  file - the same rule that produced the two entries above it.
- **2026-08-25 - the 1900 charts were apps.plugin, guessed at twice as something else.**
  A `grep -c systemd` over the charts JSON returned 1903, and that number was read as
  "1900 systemd charts" - first blamed on the cgroups plugin, then on the go.d systemdunits
  collector. It was neither: grep counts string occurrences across every field of every
  chart, not charts. Counting properly (`cut -d. -f1 | uniq -c`) put it beyond argument -
  **apps.plugin**, at 644 per-application, 168 per-user, 154 per-usergroup and 46 file-
  descriptor charts, about 90% of the total. Per-systemd-service cgroup charts were real but
  small (22 units, 7 each) and needed a different key again, `cgroups to match as systemd
  services`, because v2 dropped `enable systemd services`. Two things this bought that the
  guessing did not: the chart list also shows every configured **alarm**, which finally
  answered "what am I notified about" from evidence, and it showed the API's chart families
  briefly missing right after a restart - the cgroups plugin rediscovers containers on a 10s
  cycle, so a count taken seconds after a deploy under-reports. Measure the thing, and know
  what your measurement counts.
- **2026-08-25 - the config installer ate its own worklist.** The netdata trim above shipped,
  deployed cleanly, printed nothing, exited 0 - and installed neither file. The loop was
  `while read CFG; do ... done < <(find ...)`, so the worklist arrived on **stdin**, and
  `docker compose exec` reads stdin even with `-T`. The first iteration's `cat` drained the
  remaining filenames; the loop ended after one file. Sorted first was `go.d/httpcheck.conf`,
  already installed and byte-identical, so the one iteration that ran printed nothing either.
  `set -e` does not catch it: the loop succeeded, and the dirty-flag line after it is exempt
  as the non-final command of an `&&` list. Fixed by feeding the loop on **fd 3** and giving
  every inner command `</dev/null`. The lesson is the one this section keeps repeating in a
  new costume - **so the installer now always prints `N checked, M updated`, and exits
  non-zero if it finds nothing to check.** Silence had been indistinguishable from a healthy
  no-change run three times; it no longer is.
- **2026-09-16 - repos moved to the NotRover org; box auth switched off deploy keys.** All
  Orange Copy Paste repos were transferred from the `Spectrewolf8` account to the `NotRover`
  org (same repo names). The read-only deploy key transferred with the repo but the org
  disables deploy keys by policy ("Disabled by NotRover", no per-repo override), so `git
  fetch` failed with "Repository not found". Switched the box to a fine-grained
  **Contents: Read-only** token over HTTPS, embedded in the `origin` remote URL (section
  5.6, section 9); `deploy.sh` is unchanged because git ignores its `GIT_SSH_COMMAND` for an
  HTTPS remote. Trade-off: the token expires (max ~1 year) and must be rotated, where the
  deploy key did not. The old `~/.ssh/id_repo` key and the disabled GitHub deploy key are
  now unused and can be removed.

---

## Local Development

For a full local stack — Postgres, Redis, and MinIO standing in for R2 — see the
`docker-compose.yml` at the repo root and the setup notes in the main `README`. The dev
stack is three services (no worker service):

- `api` — FastAPI `uvicorn --reload` on `:8000`.
- `db` — `postgres:16-alpine`, the local stand-in for Supabase Postgres.
- `redis` — `redis:7-alpine`.

Blobs use MinIO or a real R2 bucket via env. You still need a real Supabase project
locally, because the backend verifies Supabase-issued JWTs and never signs its own.
