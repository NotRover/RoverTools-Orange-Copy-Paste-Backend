# RoverTools' Orange Copy Paste - Backend Deployment

**Owns:** the self-hosted OVH VPS that runs the sync backend and Redis - how to reach it,
recover it, harden it, and deploy the API onto it (Docker Compose + Caddy, deployed by a
git poll on the box). Also
the external services the backend depends on (Supabase, R2) and the migration discipline.
**Not here:** the wire contract - routes, payloads, DDL, socket events, crypto envelope -
which is [architecture.md](architecture.md); client internals (the app's own
`docs/architecture.md`); who-may-do-what (`docs/permissions.md` at the workspace root).

**Every host, domain, IP, and key value in this file is a placeholder** - `example.com`,
`203.0.113.10` / `2001:db8::1`, `your-vps-hostname`, `SHA256:REDACTED-HOST-KEY`, and the
like. Replace them with your own. This file names passwords and keys by *where they live*,
never by value. Real secrets belong in the untracked `.env` and your password manager,
never in this file or in git. Anything pasted into git history stays there forever. If a
real secret ever lands here, rotate it; editing it out is not enough.

> This is the deployment runbook for self-hosting the backend. It describes the reference
> deployment the maintainers run: Docker Compose + Caddy on a small VPS, deployed by a git
> poll on the box. Their host, domain and keys appear only as placeholders. A step-by-step
> version for newcomers is on the docs site. This setup replaced an earlier
> Render/Supabase/R2 setup.

## Table of Contents

**Part A - the box**
1. [The box](#1-the-box)
2. [Access - the part you cannot lose](#2-access---the-part-you-cannot-lose)
3. [Recovery - when SSH will not let you in](#3-recovery---when-ssh-will-not-let-you-in)
4. [What is hardened, and why](#4-what-is-hardened-and-why)
5. [Reproduce the box from scratch](#5-reproduce-the-box-from-scratch-step-by-step)

**Part B - the backend on the box**
6. [What you are deploying](#6-what-you-are-deploying)
7. [External services - Supabase and R2](#7-external-services---supabase-and-r2)
8. [Database migrations](#8-database-migrations)
9. [Deployment - build-on-box, polled from git](#9-deployment---build-on-box-polled-from-git)
10. [Verify the deployment](#10-verify-the-deployment)
11. [Point the desktop app at it](#11-point-the-desktop-app-at-it)
12. [Ongoing operations](#12-ongoing-operations)
13. [Troubleshooting](#13-troubleshooting)
14. [Decisions - settled and open](#14-decisions---settled-and-open)

---

# Part A - the box

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
| `root` | locked - no root login by any path, including the console |
| Deploy runs as | `ubuntu` in `~/app`, pulling read-only over HTTPS with a fine-grained token (section 5.6) |

---

## 2. Access - the part you cannot lose

### Normal login

From the Windows workstation:

```
ssh ubuntu@203.0.113.10
```

- Auth is **key only**. Password login over SSH is disabled.
- The private key is `~/.ssh/id_ed25519`, protected by a passphrase. SSH
  prompts for that passphrase on connect, or once per session if `ssh-agent` is running.
- Public key fingerprint: `SHA256:REDACTED-HOST-KEY`
  (comment `you@example.com`). This is the only key in `ubuntu`'s `authorized_keys`.
- To skip the passphrase prompt on every connection, load the key into `ssh-agent` once.
  In an **admin** PowerShell, run `Set-Service ssh-agent -StartupType Automatic;
  Start-Service ssh-agent`. Then, in a normal one, run `ssh-add $HOME\.ssh\id_ed25519`.
  The key stays loaded until reboot.

### Two secrets, do not confuse them

These two secrets are unrelated. Mixing them up wasted a session once:

| | Where it lives | What it unlocks | Prompt you see |
|---|---|---|---|
| **Key passphrase** | the workstation, on the key file | the private key, so SSH can use it | `Enter passphrase for key '...id_ed25519'` |
| **`ubuntu` account password** | the server (`/etc/shadow`) | the OVH console, and `sudo` if `NOPASSWD` is ever removed | the OVH console login, never SSH |

**`passwd` changes only the account password. It has no effect on SSH login**, because
SSH here authenticates by key, not by account password. A workstation prompt that says
"passphrase for key" wants the key passphrase. Typing the account password there fails.
You can change the account password freely; it never touches SSH.

**If the workstation or that key is lost, you add a new key through the recovery path
below.** Keep the `ubuntu` password safe in the password manager. It is not an SSH path,
but it is the console path, and the console is how you install a replacement key.

### Verifying you are talking to the real box

On a first connection, or if SSH ever warns about a changed host key, the fingerprint must
match one of these (captured and verified out-of-band on 2026-08-24):

```
ED25519  SHA256:REDACTED-HOST-KEY
ECDSA    SHA256:REDACTED-HOST-KEY
RSA      SHA256:REDACTED-HOST-KEY
```

A mismatch means the box was rebuilt or someone is between you and it. Do not type a
passphrase until you know which.

---

## 3. Recovery - when SSH will not let you in

Ordered from least to most drastic. Try them in order.

### 3a. OVH KVM console (the main escape hatch)

The console works even when sshd, the firewall or the network config is broken. It is a
virtual monitor and keyboard, not a network service.

1. OVH panel -> Bare Metal Cloud -> VPS -> this VPS -> actions menu (`...`) -> **Console**.
2. Log in as `ubuntu` with its password (password manager). `root` is locked, so it is not
   an option here.
3. Fix whatever broke. Then log in over SSH from a *new* terminal. The fix is done when
   that login succeeds.

Confirm the console accepts the `ubuntu` password **while SSH is healthy**, not for the
first time during an outage. An untested escape hatch is not an escape hatch.

### 3b. Restore a lost admin key

If the workstation key is gone, get in through the console (3a) and append a new public
key. SSH is locked to named users, so also make sure the account is still allowed:

```
echo 'ssh-ed25519 AAAA...newkey... comment' >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
# only if you add a NEW user rather than reusing `ubuntu`:
sudo sed -i 's/^AllowUsers .*/& newuser/' /etc/ssh/sshd_config.d/01-hardening.conf
sudo sshd -t && sudo systemctl restart ssh.socket
```

### 3c. OVH rescue mode (last resort)

If the console itself is unusable, boot the VPS into OVH rescue mode from the panel. Rescue
mode brings up a temporary system with the real disk unmounted. Mount the disk by hand to
repair `/etc/ssh`, `authorized_keys` or a broken `/etc/fstab`. This is the slowest path;
everything above exists to avoid it.

### The golden rule for every change to SSH or the firewall

**Keep the working session open. Prove the change in a brand-new session before you close
the first one.** sshd here is socket-activated (`ssh.socket`), so each connection is its own
process. A bad config cannot kill the session you are sitting in; it only breaks *new*
logins. That open session is your safety line. Keep it until a fresh login succeeds.

---

## 4. What is hardened, and why

Everything here is **done and verified**. The exact commands to rebuild it are in section 5.

### SSH - key-only, root-off, named users

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

- **`01-` prefix, not `99-`.** `sshd_config` is first-match-wins, and its
  `sshd_config.d/*.conf` include sits at the top of the file. Drop-ins are read in
  alphabetical order, and the *earliest* one to set a keyword wins. The stock files here are
  `50-cloud-init.conf` (which shipped `PasswordAuthentication yes`) and
  `60-cloudimg-settings.conf`. A `99-` file loses every conflict silently and looks like it
  did nothing.
- **`KbdInteractiveAuthentication no` matters as much as `PasswordAuthentication no`.**
  PAM keyboard-interactive left on is a live password path. It makes you *think* you are
  key-only when you are not.
- **`AllowUsers ubuntu`** turns every username-guessing attempt into an instant reject. Only
  `ubuntu` logs in over SSH. The deploy runs as `ubuntu` from a local timer and needs no
  inbound access (section 9). Any user you add later must be added to this line too (see
  3b). The box still lists `deploy` here from the earlier push-deploy design; drop it.
- Port stays 22. Moving it only cuts log noise. On socket-activated sshd the port lives in
  `ssh.socket`, not `sshd_config`, so editing the obvious file changes nothing.

### Firewall - deny by default

`ufw` denies incoming and allows outgoing by default. Only 22, 80 and 443 are open, on both
IPv4 and IPv6. `IPV6=yes` is set.

**Nothing opens 6379.** Redis runs inside the compose network with no published port. The
firewall is ufw on the box, not OVH's edge firewall.

**Why:** the box has a public IPv6 and sshd listens on `[::]:22`, so an IPv4-only firewall
would leave v6 wide open. For Redis, the firewall is a second lock on a door that is not
there. The OVH edge firewall is stateless, has rule-count limits and is easy to
half-configure into a break.

### Brute-force + patching

- **fail2ban**, `/etc/fail2ban/jail.local`: `backend = systemd`, `banaction = ufw`, 1h ban
  after 5 fails in 10m, `sshd` jail on.
  - `backend = systemd` is load-bearing. Ubuntu has not shipped rsyslog by default since
    24.04, so `/var/log/auth.log` never appears and the stock jail watches a missing file.
    Reading the journal is the only thing that works.
  - `banaction = ufw` because the default `iptables-multiport` writes rules that fight the
    ufw chains.
  - With password auth off this is mostly log hygiene, but it works: it banned a real
    scanner within seconds of starting. Public port 22 gets constant automated probing. A
    nonzero `Total banned` is normal background noise, not a targeted attack.
  - **It can ban *you*.** 5 failed auths in 10 minutes from any IP, including yours, is a
    1h ban. Repeatedly mistyping the key passphrase can trip it. The symptom is SSH timing
    out or refusing *before* the passphrase prompt. A ban blocks the TCP connection, so if
    you still get the prompt you are not banned. Check and recover:
    ```
    echo $SSH_CONNECTION                 # first field = your current client IP
    sudo fail2ban-client status sshd     # is your IP in the banned list?
    sudo fail2ban-client unban --all     # clear all bans (run from the KVM console if SSH is refused)
    ```
    Only unban an IP you have confirmed is yours; leave scanner bans in place.
- **unattended-upgrades** on, with `Automatic-Reboot "true"` at **04:30 UTC**
  (`/etc/apt/apt.conf.d/52unattended-reboot.conf`). Security origins include
  `resolute-security` and ESM. That reboot briefly drops every WebSocket sync connection,
  and clients reconnect on their own. Narrow it to reboot-only-when-required if that blip
  ever matters.

### Docker + who runs the deploy

- **Docker** comes from Ubuntu's own packages (`docker.io 29.1.3`, `docker-compose-v2
  2.40.3`), not Docker's apt repo. `ubuntu` is in the `docker` group. `docker-buildx` is
  left out; the plain builder is enough for the on-box build.
- **The deploy runs as `ubuntu`.** The login user owns the checkout (`~/app`) and runs the
  deploy timer. The box reaches **out** to GitHub with an **outbound, read-only**
  fine-grained token over HTTPS (section 5.6), and nothing reaches in. `.env` lives inside
  the checkout, git-ignored, mode 600. A `git reset --hard` leaves ignored files alone.
  - **Historical:** earlier designs added a separate `deploy` user. The first had an
    inbound, forced-command-locked CI key (`id_ci`) for a GitHub Actions push-deploy; the
    second was a no-SSH service account. Both were dropped for the simpler "run as
    `ubuntu`, poll git" model. On a box carrying that user, remove it: `sudo userdel -r
    deploy`, drop `deploy` from `AllowUsers`, `sudo rm -rf /opt/rovertools`.

**Why:** Ubuntu's Docker packages get security patches through the unattended-upgrades
already set up, with no third-party repo or key to maintain. Deploys are **polled**, so
nothing logs in to deploy, and a dedicated service account buys little.

---

## 5. Reproduce the box from scratch, step by step

This is the path taken on 2026-08-24, mistakes included. Each step says how to **verify**
it before moving on, and what **bit us** where something did. Run the steps in order; the
order is load-bearing. Installing the key before disabling passwords is the whole reason
step 5.1 comes before 5.2. Section 4 holds the reasoning behind each hardening choice; this
section holds the steps.

**The golden rule applies to every SSH/firewall step:** keep the working session open, and
prove the change in a brand-new session before closing it (section 3).

**The whole path, in order.** This list covers everything below plus the stack sections, so
a rebuild is one list rather than a hunt. Nothing here is optional except where marked:

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
setup beyond step 16. Netdata's collector and notification config live in `netdata/conf/`
in the repo, and the deploy installs them, so a rebuilt box arrives already watching itself.

### 5.0 First access (OVH)

Provision VPS-1 with Ubuntu 26.04. OVH creates the `ubuntu` account with **passwordless
sudo** and leaves `root` **locked**. You never log in as root, by any path. The first login
is the only time you use a password over SSH, because key auth is not set up yet:

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
  accepts the `ubuntu` password. This is your escape hatch (section 3a). Test it before you
  need it, not during an outage.
- **What bit us:** we assumed key auth was already configured. It was not. Every early
  login was by password, and `ubuntu`'s `authorized_keys` was empty. That is what makes
  5.1-before-5.2 non-negotiable.

### 5.1 Install and prove your SSH key (before any lockdown)

Run the first command on the Windows workstation. `~` does not expand in some Windows
shells; if it fails, use the absolute path to the key:

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

**Verify - do not proceed until this passes:** from a *new* terminal, `ssh ubuntu@<ip>`
logs in **by key**. The prompt it shows is `Enter passphrase for key` (the key passphrase),
not a password. If it still asks for a password, the key is not installed correctly.

### 5.2 SSH lockdown

**Warning:** this step disables password login. Run it only after 5.1 proves key login, or
you lock yourself out. The `01-` prefix and `KbdInteractiveAuthentication no` both matter;
section 4 says why.

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

**Verify:** open a *new* terminal and confirm `ssh ubuntu@<ip>` still works. Then confirm
password auth is dead: `ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no
ubuntu@<ip>` must be refused. Only then close the original session. sshd is socket-activated
(`ssh.socket`), so a bad config only breaks *new* logins, and your open session survives it.

**What bit us:** this step locked us out the first time, because `authorized_keys` was
empty (5.0). We recovered by pasting the key in through the still-open session, then
confirming a fresh key login.

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
v4 and `(v6)`. `IPV6=yes` in `/etc/default/ufw` (Ubuntu's default) is what creates the v6
rules. From the workstation, `Test-NetConnection <ip> -Port 6379` must report
`TcpTestSucceeded : False`.

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

**Verify:** `sudo fail2ban-client status sshd` returns a jail status. That proves `backend =
systemd` is reading the journal. The stock file backend would fail here, because modern
Ubuntu ships no `/var/log/auth.log`.

**What bit us:** fail2ban bans *any* IP with 5 failed auths in 10 minutes, **including
yours** (section 4 has the symptom and the unban commands). A scanner (198.51.100.20) was
banned within seconds of starting the jail. That is normal background noise on a public
port 22, not a targeted attack.

### 5.5 Docker

Install Ubuntu's own packages, patched by the unattended-upgrades set up in 5.4. Leave out
`buildx`: images build on the box with the plain builder (section 9).

```bash
sudo apt install -y docker.io docker-compose-v2
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu
newgrp docker            # activate the group in THIS shell; a fresh login does it permanently
docker run --rm hello-world
```

**Verify:** `docker run --rm hello-world` prints `Hello from Docker!`.

**What bit us:** running `docker ps` right after `usermod` gave `permission denied ...
docker.sock`. The new group is not active in the shell that added it. `newgrp docker`, or
logging out and back in, fixes it. `docker` group membership is root-equivalent.

### 5.6 Read-only pull token (HTTPS)

The deploy runs as `ubuntu` (section 4) and only ever pulls. The box needs read-only
outbound access to the private repo and nothing more.

**Why not a deploy key.** The original design used an SSH deploy key. The `NotRover` org
disables deploy keys by policy: the repo's Deploy keys page shows "Disabled by NotRover",
with no per-repo override. The box authenticates with a **fine-grained personal access
token** over HTTPS instead. Its security profile matches the old key: read-only, one repo,
stored only on the box.

Create the token in GitHub. Owner **NotRover** approves it, since it targets an org repo:

- Settings -> Developer settings -> Fine-grained tokens -> Generate new token.
- Resource owner **NotRover**; repository access limited to
  `RoverTools-Orange-Copy-Paste-Backend`; Repository permission **Contents: Read-only**.
- Fine-grained tokens must expire (max ~1 year). Set a reminder to rotate before then. An
  expired token makes every deploy poll fail on `git fetch` until it is replaced.

The token is embedded in the `origin` remote URL on the box (section 9), so it lands in
`~/app/.git/config`. `chmod 600` that file. Section 9 covers the rest of the wiring
(cloning the repo, `.env` and the systemd timer), since it depends on the repo files.

**Historical:** an earlier design added a separate `deploy` user. Section 4 says how to
remove it from a box provisioned under that design.

### 5.7 Domain (FreeDNS)

Caddy gets its TLS cert for this name. At `freedns.afraid.org`, add an **A** record for a
subdomain pointing at the box's IPv4. The API is `api.example.com` -> `203.0.113.10`,
and the status dashboard is `status.example.com` -> the same box.

**Verify:** `nslookup api.example.com 8.8.8.8` returns `203.0.113.10`.

**What bit us:** the first record pointed at the wrong IP; fix the A record's destination in
the FreeDNS panel. DNS caches negative answers. A resolver queried too early can hold a
stale "no such name" for a while; Cloudflare's `1.1.1.1` did this. Check on the
authoritative nameserver or `8.8.8.8`. This is harmless for TLS: Let's Encrypt validates
from its own resolvers, not a public cache.

### 5.8 Persistent, capped journal for container logs

The containers log to the systemd journal (`journald` driver, section 9), so the logs outlive
the container swaps a deploy makes. Set two things once: make the journal persistent (on
disk, not only in RAM), and cap it so it can never fill the disk.

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
succeeds (persistent storage is active). Without this, `Storage=auto` keeps logs in a
volatile ring buffer that a reboot wipes.

The Docker daemon writes container logs into the **system** journal, which an ordinary user
cannot read. Until `ubuntu` can read it, `journalctl -t rovertools-api` shows
`-- No entries --`. Add `ubuntu` to the log groups; this takes effect on the next login:

```bash
sudo usermod -aG adm,systemd-journal ubuntu
```

Until you re-login, prefix reads with `sudo`. `sudo journalctl -t rovertools-api` always works.

---

### 5.9 Swap file

The VPS ships with **no swap at all** (`free -h` shows `Swap: 0B`). This step adds a 2 GB
swap file on the 38 GB disk.

**Why:** memory is not short. The box idles around 940 MB used of 3.7 GB, with 2.8 GB
available, and all five containers together use under 300 MB. The problem is runway. With
no swap, the kernel goes straight from "fine" to the OOM killer choosing a victim, and the
largest target on this box is the API container. Swap turns "the API is terminated
mid-request" into "things get briefly slow".

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
swapon --show && free -h
```

Make it survive a reboot. **`nofail` matters.** Without it, a missing or corrupt swap file
can hold up boot. If the box does not come up, the only way back in is the console
(section 3). Check `/etc/fstab` before you trust a reboot:

```bash
echo '/swapfile none swap sw,nofail 0 0' | sudo tee -a /etc/fstab
sudo findmnt --verify --verbose | tail -5   # sanity-check fstab BEFORE trusting a reboot
```

Then set swappiness to 10, so the kernel treats swap as an emergency reserve. The default
of 60 swaps out idle pages while RAM is free, which is the wrong trade for a
latency-sensitive API:

```bash
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swappiness.conf
sudo sysctl --system | grep -i swappiness
```

Swap in use is a **signal, not a solution**. If Netdata shows swap consistently occupied,
something is growing. Find it rather than adding more swap.

Redis is **not** installed on the host; it ships as a compose service (section 9). The box
is now hardened and ready to deploy. Section 9 covers the application stack (Docker
Compose, Caddy and the deploy timer).

---

# Part B - the backend on the box

## 6. What you are deploying

The backend has four moving parts. Only the first is code you ship. The last two are
external and did not change in the move onto the VPS:

| Component | Runs on | Purpose |
|---|---|---|
| **FastAPI service** | VPS, Docker (`api`, one replica today) | The API + `/ws` realtime endpoint |
| **Redis** | VPS, Docker (`redis`) | WebSocket pub/sub fan-out + device presence |
| **Postgres + Auth** | Supabase (external) | Ciphertext store; issues the JWTs we verify |
| **Blob storage** | Cloudflare R2 (external) | Encrypted image/file blobs via presigned URLs |

Redis is **not optional**: realtime fan-out and presence depend on it. It holds only
ephemeral state (pub/sub + presence), so it needs no persistence.

The service is **stateless**, so it can run several replicas. Cross-replica delivery goes
through Redis pub/sub: a client connected to replica A still receives events published by
replica B. The production stack runs one `api` container, and a deploy recreates it
(section 9).

---

## 7. External services - Supabase and R2

Supabase owns identity and stores ciphertext. The backend only **verifies** its tokens and
never signs one. R2 stores encrypted blobs. Neither moved when the backend left Render.

### 7a. Supabase

Create a project, then collect these four values.

> **Dashboard note:** Supabase reorganised these screens in 2025. API keys now live under
> **Settings -> API Keys** (also in the **Connect** dialog), and JWT configuration under
> **Settings -> JWT Keys**. Older guides pointing at "Settings -> API -> JWT Secret" are
> stale.

| Value | Where | Env var |
|---|---|---|
| Connection string (URI) | Settings -> Database | `DATABASE_URL` |
| Project URL | Settings -> API Keys | `SUPABASE_URL` |
| Secret key (`sb_secret_...`) | Settings -> API Keys | `SUPABASE_SERVICE_ROLE_KEY` |
| Legacy JWT secret | Settings -> JWT Keys | `SUPABASE_JWT_SECRET` *(usually blank - see below)* |

**Direct host vs pooler.** Supabase's direct host (`db.<ref>.supabase.co`) resolves to
**IPv6 only**. The VPS has outbound IPv6, so unlike the old Render host it *can* use the
direct host. The **Supavisor pooler** (Connect dialog -> Session pooler) is still the
safer default. It is IPv4-reachable on every tier and does not depend on v6 routing staying
healthy. Rewrite the driver to asyncpg; the username carries the project ref:

```
postgresql+asyncpg://postgres.<project-ref>:<password>@aws-<region>.pooler.supabase.com:5432/postgres
```

| Mode | Port | Notes |
|---|---|---|
| **Session** (recommended) | 5432 | Behaves like a normal connection; the app already pools |
| Transaction | 6543 | Scales to more clients; **no prepared statements** |

Session mode fits, because the app maintains its own SQLAlchemy pool. In transaction mode,
`src/database.py` detects port `6543` and disables asyncpg's statement caches. Without
that, you would hit `prepared statement does not exist` under load.

**About the JWT secret - the part that trips people up.** Supabase has signed access tokens
with **asymmetric keys (ES256) by default since 2025-10-01**. The backend detects the
algorithm per token:

- **New project (2025-10-01 or later)** -> leave `SUPABASE_JWT_SECRET` **blank**. Tokens are
  verified against the project's JWKS endpoint, derived from `SUPABASE_URL`. The backend
  picks up key rotation on its own, with no redeploy.
- **Older project still signing HS256** -> set `SUPABASE_JWT_SECRET` to the legacy secret.
- **Mid-migration** -> both work at once; the JWKS carries the legacy secret alongside the
  new key.

`SUPABASE_SERVICE_ROLE_KEY` accepts either a new secret key (`sb_secret_...`) or the legacy
`service_role` key. Supabase deprecates the legacy keys at the **end of 2026**, so prefer a
secret key. It is server-only; never ship it to a client.

### 7b. Cloudflare R2

1. Create a bucket (e.g. `clipboard-blobs`) -> `S3_BUCKET`.
2. Create an R2 API token -> `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY`.
3. Copy the account endpoint -> `S3_ENDPOINT_URL`
   (`https://<account-id>.r2.cloudflarestorage.com`).
4. `AWS_REGION=auto`.

The bucket **must already exist**. The service only presigns URLs; it never creates
buckets. Text and note sync work without R2. Only large image and file attachments need it.

---

## 8. Database migrations

**Migrate deliberately, and never take a green deploy as proof that anything migrated.** The
deploy pipeline (section 9) ships code only; it never runs a migration.

Nothing in the app's startup path migrates either, by design. Read the database's revision;
never infer it.

### The Migrate database workflow

[`.github/workflows/migrate.yml`](../.github/workflows/migrate.yml) is the normal way to
migrate. It holds the production `DATABASE_URL`, so nobody has to paste one.

**Set up the `production` environment once.** The URL is an environment secret, not a
repository secret, so only a run from `main` can read it:

1. Repository **Settings > Environments > New environment**, name it `production`.
2. Under **Deployment branches and tags**, choose **Selected branches and tags** and add
   `main`.
3. Under **Environment secrets**, add `DATABASE_URL` (the session pooler URI on port 5432).
4. Delete the old repository secret of the same name (**Settings > Secrets and variables >
   Actions**), so no workflow can reach it outside the environment.

Until step 3 is done, every run fails at its first check with "DATABASE_URL is not set on
the production environment", and nothing touches the database.

The workflow runs itself, read-only, on any push to `main` that touches `migrations/**`. It
posts the pending DDL to the run summary. Applying is always a separate, deliberate
dispatch. **Warning:** this command changes the production schema; read the preview first:

```bash
gh workflow run migrate.yml -f action=upgrade -f revision=head -f confirm=migrate
```

`action` has three values and only one of them writes:

| `action` | Does |
|---|---|
| `current` | Reports the revision and what is pending. Changes nothing. **The default** |
| `preview-sql` | Prints the SQL an upgrade would run. Changes nothing. |
| `upgrade` | Applies it. Also needs `confirm=migrate`, or it refuses. |

Every run reports where the database stands. A read-only run that finds pending revisions
says so as a warning and prints the dispatch that would apply them. An `upgrade` re-reads
the revision afterwards and fails if the database did not move.

**Why:** the failure worth designing against is a green run that looks like the fix and
was not.

To see which revision the database is on, run the workflow with `action=current`. It
changes nothing. The last recorded upgrade was to `0020`, on 2026-09-26. A revision added after
that has not reached the database until someone applies it.

### By hand

This is equivalent, for a database the workflow holds no credentials for. **Warning:**
`alembic upgrade` changes whatever database `DATABASE_URL` names:

```bash
uv sync
DATABASE_URL="postgresql+asyncpg://postgres:<pw>@db.<ref>.supabase.co:5432/postgres" uv run alembic upgrade head
```

PowerShell has no inline env prefix, so there it takes two statements:

```powershell
$env:DATABASE_URL = "postgresql+asyncpg://postgres:<pw>@db.<ref>.supabase.co:5432/postgres"
uv run alembic upgrade head
```

`migrations/env.py` reads `DATABASE_URL` from the environment and ignores `.env`. When the
variable is unset, it falls back to the localhost URL in `alembic.ini`, so an unset
variable quietly migrates your own machine. Check where you point before running, and read
the applied revision rather than inferring it:

```bash
uv run alembic current
```

`pytest` cannot catch a missing migration. The harness builds its schema with
`Base.metadata.create_all`, so a table can exist for every test and still be absent from a
real database.

---

## 9. Deployment - build-on-box, polled from git

**No GitHub Actions and no image registry, on purpose.** The image builds on the VPS, and
GitHub's only job is hosting the repo, which the box pulls read-only.

**Why:** a private repo's Actions minutes (2,000/mo) and its GHCR/Packages storage (500 MB
free) both cost money once builds accumulate; the storage is the real trap. Render hid
that by building on its own machines. Building on the VPS you already pay for spends
nothing on GitHub.

**Status: live on the box since 2026-08-24** (merged to `main`, deployed and verified). The
wiring below was done once. The repo files are the source of truth; this section explains
them and the parts that live nowhere else.

### How a deploy flows

The box **reaches out** to GitHub; nothing reaches in. A systemd timer runs `deploy.sh` on a
~90s poll:

```mermaid
flowchart LR
    PUSH["push to main"] --> GH["GitHub<br/>(repo only)"]
    TIMER["systemd timer<br/>(~90s poll)"] --> DEPLOY["deploy.sh"]
    DEPLOY -- "git fetch<br/>(read-only token over HTTPS, outbound)" --> GH
    DEPLOY --> MOVED{"origin/main<br/>moved?"}
    MOVED -- "no" --> NOOP["exit<br/>(quiet no-op, the common case)"]
    MOVED -- "yes" --> BUILD["git reset --hard<br/>docker compose build api<br/>docker compose up -d (recreate api)"]

    classDef event fill:#20140f,stroke:#ff3e1c,stroke-width:2px,color:#fafafa
    classDef step fill:#1b1b1b,stroke:#9a9a9a,stroke-width:1.5px,color:#fafafa
    classDef store fill:#161616,stroke:#6f6f6f,color:#e4e4e4
    classDef decide fill:#20140f,stroke:#ff3e1c,stroke-width:2px,color:#fafafa
    class PUSH,TIMER event
    class DEPLOY,NOOP,BUILD step
    class GH store
    class MOVED decide
```

- The image is **built on the box** from the checkout and tagged `rovertools-api:<short-sha>`
  (plus `:latest`). It never leaves the box: no registry, no push, no Actions.
- Deploys land within a couple of minutes of a push. The box has no inbound endpoint, no
  webhook secret and no CI credentials. The attack surface stays "outbound git + Docker".
- Config travels with the code. `docker-compose.prod.yml`, `caddy/Caddyfile` and `deploy.sh`
  all live in the checkout, so a change to any of them ships on the next poll, as app code
  does. The Caddyfile needs a separate reload step in `deploy.sh`; see "The files" below.

### Signed commits only

`deploy.sh` checks the signature on the commit it is about to deploy against
`deploy/allowed_signers`, before `git reset --hard`. A commit that no listed key signed is
not deployed, and the run fails with a red Discord embed. This is what stops a stolen GitHub
token or account from putting code on the box.

**It protects nothing until you add a key.** While the file lists no keys, every deploy logs
`WARNING: deploy/allowed_signers lists no keys` and carries on unchecked. That kept the
deploy that introduced the check from breaking the pipeline.

To turn it on:

1. On each machine you push from, sign commits with an SSH key:

   ```bash
   git config --global gpg.format ssh
   git config --global user.signingkey ~/.ssh/id_ed25519.pub
   git config --global commit.gpgsign true
   ```

2. Add each key to `deploy/allowed_signers`, one line per key, using the email in your
   commits:

   ```
   you@example.com namespaces="git" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA...
   ```

3. Commit that change **signed**, and check it locally before pushing:

   ```bash
   git -c gpg.ssh.allowedSignersFile=deploy/allowed_signers verify-commit HEAD
   ```

The box reads the file from the checkout it is already running, not from the incoming
commit, so a commit cannot vouch for itself by adding its own key. It also means the commit
that first adds your key is deployed unchecked. Every commit after it is checked.

**Merging on GitHub breaks this.** A squash or merge commit made with the GitHub merge
button is signed by GitHub's own GPG key, which `allowed_signers` does not hold. "Rebase and
merge" drops signatures. Merge locally (`git merge --ff-only` of a signed branch, or a
signed merge commit) and push `main` yourself.

### What "zero downtime" means here

**There is none, and that is a deliberate choice.** There is one `api` container and a deploy
recreates it, so every deploy has a **~1-3s window with no backend**:

- **HTTP** - a request landing in that window gets a **502**. This was measured: a
  watch-curl across `deploy.sh --force` shows two 502s (one immediate, one ~3s dial timeout)
  with 200s either side. Clients retry and deploys are infrequent, so the gap rarely meets a
  real request.
- **WebSockets** - open sockets drop once when the old container is removed, and clients
  reconnect. The presence heartbeat (`PING_INTERVAL = 25s`, `src/realtime.py`) keeps a socket
  under any 100s idle timeout and re-`SET`s presence on reconnect.

**What was tried and rejected.** True zero-gap needs two `api` containers overlapping. Every
route to that was declined:

- **`docker-rollout`** - a third-party single-file script running with Docker (root) access.
  Declined on trust. `deploy.sh` briefly guarded a `docker rollout` branch, and the guard
  misfired once the plugin was absent. `docker <unknown> --help` exits 0, so the branch ran
  and failed the deploy with `unknown shorthand flag: 'f'`. The branch is gone;
  reintroducing overlap means editing `deploy.sh` deliberately.
- **Docker Swarm** - native start-first updates, but a cluster orchestrator on a single host.
  `docker stack deploy` also cannot build, which fights the build-on-box model.
- **Caddy `lb_try_duration` retry** - looked like a free win and **does not work here**. The
  config was confirmed live (`caddy adapt` showed `try_duration: 10000000000`), and the swap
  still 502'd identically. Retry and failover pick another *healthy host in a pool*. With
  one container the pool is momentarily empty, so there is nothing to fail over to. The
  directive was removed rather than left implying protection it does not give.

If the gap ever matters, the fix is overlap (Swarm, or a vendored and audited rollout
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

- **Caddy** - the only container with published ports (80/443). It provides automatic TLS,
  proxies WebSockets with no extra config and serves both hostnames. A deploy never
  recreates it.
- **api** - built locally from the `Dockerfile`, with **no** host port. Only Caddy reaches it
  over the compose network. One replica; a deploy recreates it.
- **redis** - internal network, **no published port**, `requirepass`, persistence off. An
  `api` deploy does not recreate it, so presence is not needlessly flushed.
- **netdata** - metrics agent, **no** host port, behind Caddy basic auth. It reads the host
  read-only (`/proc`, `/sys`, `/var/log` and a few files under `/etc`; section 12). It gets
  container names from **dockerproxy**, a read-only allowlisted Docker socket proxy that is
  not web-facing. It watches the stack from inside it, which is why an external check still
  matters (section 12).

### The files (backend repo)

All under `orange-copy-paste-clipboard-backend/`. Read them for detail; the non-obvious parts:

- **`Dockerfile`** - builds from `uv.lock` with `uv sync --frozen`, so a build ships the
  exact locked versions. It runs as a non-root user, with a `HEALTHCHECK` on
  `/internal/healthz`. That endpoint returns 200 whenever the process can serve. A degraded
  Postgres or Redis shows in the body, not the status code (`src/admin/router.py:137`).
  That makes it the right liveness signal for a swap. `.dockerignore` keeps
  `.git`/`.venv`/tests/docs out of the build context.
- **`docker-compose.prod.yml`** - `caddy` (published 80/443), `api` (`build: .`, tagged
  `${IMAGE}`, no host port), `redis`, `netdata` and `dockerproxy` (no host ports). The dev
  `docker-compose.yml` is separate.
- **`caddy/Caddyfile`** - `api.example.com` and the status site, auto TLS. The `dynamic a`
  upstream (via Docker DNS `127.0.0.11`) re-resolves `api` per request. After a recreate,
  Caddy finds the new container's IP instead of caching the dead one. It has no retry
  directives; "What zero downtime means here" says why they do not help with one container.
- **`deploy/deploy.sh`** - the poll, build and deploy script, run from the checkout by the
  timer. It **re-execs itself** when the pull changed it. Bash reads a script from the
  handle it opened at startup. Without the re-exec, a change to this file lands only on the
  next poll, and a step added here does nothing on the deploy that introduced it.
  After `up -d` it also **validates and reloads Caddy** (`caddy validate`, then `caddy
  reload`). `up -d` does not recreate a container whose only change is its mounted config,
  so a Caddyfile edit would otherwise recreate nothing.
  It **reports on itself to Discord**: green on a deploy, red on any non-zero exit through
  an `EXIT` trap (section 12). It keeps the last 5 tagged images for rollback.
- **`deploy/rovertools-deploy.{service,timer}`** - the systemd units that poll ~every 90s.
- **`netdata/conf/**`** - Netdata config, mirroring `/etc/netdata/`: agent settings and the
  noise trim in `netdata.conf`, collector jobs in `go.d/`, notifications in
  `health_alarm_notify.conf`. `deploy.sh` installs it into the `netdataconfig` volume
  instead of bind-mounting it (section 12), so it still ships from git.

**Why the Redis flags** (`--save "" --appendonly no --requirepass --maxmemory 256mb
--maxmemory-policy volatile-ttl`):

- Persistence is off because Redis holds only pub/sub + presence. An RDB would only burn IO
  writing data that nothing reads after a restart.
- `requirepass` is set even with no published port, because any process on the compose
  network can reach Redis.
- `volatile-ttl` eviction is safe *only because* the backend re-`SET`s presence keys on
  every heartbeat pong (backend commit `4f841d0`), so an evicted key self-heals.

`REDIS_URL` in `.env` is `redis://:<password>@redis:6379/0`.

### One-time box wiring

The deploy runs as the login user (`ubuntu`), already in the `docker` group, in `~/app`.
There is no separate service account (section 4 says why). The box reaches **out** to
GitHub with a read-only token over HTTPS (section 5.6); nothing reaches in.

```bash
# 1. Create a read-only fine-grained token (section 5.6) and keep it handy as $TOKEN.
#    Owner NotRover, repo RoverTools-Orange-Copy-Paste-Backend, Contents: Read-only.

# 2. Clone the repo into ~/app over HTTPS with that token, then lock down .git/config:
git clone "https://x-access-token:${TOKEN}@github.com/NotRover/RoverTools-Orange-Copy-Paste-Backend.git" ~/app
chmod 600 ~/app/.git/config
#   The token is now embedded in the origin remote URL; deploy.sh's git fetch uses it as-is.
#   To rotate: git -C ~/app remote set-url origin "https://x-access-token:<new>@github.com/NotRover/RoverTools-Orange-Copy-Paste-Backend.git"

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
elsewhere. If the box still carries the retired push-deploy `deploy` user, remove it now.
**Warning:** `userdel -r` and `rm -rf` delete that user's home and files for good. Run
`sudo userdel -r deploy`, drop `deploy` from sshd's `AllowUsers` (section 4), and run
`sudo rm -rf /opt/rovertools`.

### Secrets

Secrets are never in the image and never in git. They live in `~/app/.env`
(`/home/ubuntu/app/.env`), mode 600, read by compose `env_file`. The set the app expects,
from `.env.example`:

- `DATABASE_URL` - Supabase pooler URI (section 7a).
- `REDIS_URL` = `redis://:<password>@redis:6379/0`; `REDIS_PASSWORD` also set for the redis
  service's `--requirepass`.
- `SUPABASE_URL`, `SUPABASE_JWT_AUDIENCE`, (`SUPABASE_JWT_SECRET` only for a pre-2025-10
  project), `SUPABASE_SERVICE_ROLE_KEY`.
- `S3_ENDPOINT_URL`, `S3_BUCKET`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`.
- `BREVO_API_KEY`, `EMAIL_FROM` (or the SMTP set).
- `ADMIN_API_KEY`.
- `APP_ENV=production`, `DOCS_ENABLED=false`. Both are the defaults now, so an unset value
  never switches on development behaviour.
- `DATABASE_URL`, `REDIS_URL`, `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` have no
  default: the API refuses to start without them.
- `METRICS_AUTH_USER`, `METRICS_AUTH_HASH` - read by **Caddy**, never by the app. Basic auth
  for the Netdata dashboard (section 12). Caddy refuses to start without the hash, on purpose.
- `METRICS_ALLOW_CIDR` - read by **Caddy**. Optional source allow-list for the status site
  (section 12).
- `ALERT_DISCORD_WEBHOOK` - read by Netdata and `deploy.sh`. Optional; unset means no
  Discord messages (section 12).
- **`PUBLIC_BASE_URL=https://api.example.com`** - the base of every user-facing link
  (invites, password-reset redirect). `APP_CORS_ORIGINS` already lists the Tauri client
  origins and does not change.

`FORWARDED_ALLOW_IPS` is **not** in `.env`. `docker-compose.prod.yml` sets it to Caddy's
fixed address on the `edge` network, the only peer whose `X-Forwarded-For` the API
believes. The per-IP rate limits and the admin lockout key on the address that yields, so it
must never be `*`.

### Rollback

Every build is tagged `rovertools-api:<short-sha>`, and `deploy.sh` keeps the last 5 on the
box. A rollback redeploys an earlier one: the same recreate, with the same ~1-3s gap:

```bash
cd ~/app
IMAGE=rovertools-api:<old-sha> docker compose -f docker-compose.prod.yml up -d api
# or, if that image was already pruned, check out the commit and rebuild:
#   git checkout <old-sha> && deploy/deploy.sh --force   (then `git checkout main` when done)
```

The poll moves you back to `origin/main` on its next tick. For a lasting rollback, revert
the commit on `main`, or stop the timer while you investigate:
`sudo systemctl stop rovertools-deploy.timer`. If a rollback also needs a schema revert,
that is a separate, deliberate `alembic downgrade` run by hand (section 8); `migrate.yml`
has no downgrade action. The real protection is expand/contract (section 12).

---

## 10. Verify the deployment

```bash
curl -i https://api.example.com/internal/healthz
```

Check, in order:

- **`/internal/healthz`** returns 200 with Postgres and Redis both healthy; TLS cert valid.
- An **`X-API-Version`** header is present on every response.
- **`/api/docs`** returns 404 unless `DOCS_ENABLED=true`. Leave it off in production,
  because the schema maps the admin surface as well as the client one.
- **Auth works end to end.** Sign in through Supabase, then call an authenticated route with
  `Authorization: Bearer <jwt>` and `X-Device-Id: <id>`. A 401 here almost always means a
  JWT config mismatch (section 13).
- **WebSocket connects and stays open.** Open `wss://api.example.com/ws`, send the auth
  message first, and expect `auth_ok` back. A close with code 4401 means the token or the
  device was refused. The message shapes are in [architecture.md](architecture.md).
- **`/internal/*` is closed to the internet**: `curl -i https://api.example.com/internal/v1/stats`
  returns 404 from Caddy.
- If `ADMIN_API_KEY` is set, `/internal/metrics` answers 200 **from inside the box** (503
  means the key is unset):
  `docker compose -f docker-compose.prod.yml exec api python -c "import os,urllib.request as u; print(u.urlopen(u.Request('http://127.0.0.1:8000/internal/metrics', headers={'X-Admin-Key': os.environ['ADMIN_API_KEY']})).status)"`

Also run the drills. **Deploy:** push a trivial change, or run `deploy/deploy.sh --force`,
and watch the swap. Measure the gap rather than assume it:

```bash
cd ~/app && ./deploy/deploy.sh --force >/tmp/deploy.log 2>&1 &
while kill -0 $! 2>/dev/null; do
  curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" https://api.example.com/internal/healthz
  sleep 0.3
done
```

Expect 200s with **two 502s** at the recreate (one immediate, one ~3s dial timeout). That is
the known, accepted window, not a regression. Then drill a **rollback** (redeploy the
previous SHA) and a **reboot**. **Warning:** `sudo reboot` drops every live connection.
After it, confirm the stack returns on its own through `restart: unless-stopped`.

---

## 11. Point the desktop app at it

The client holds all key material and does all encryption; the server only ever sees
ciphertext. The client needs the API base URL, and Supabase credentials for sign-in. It
authenticates against Supabase directly and forwards the access token as an opaque string.
It never inspects the JWT, so the asymmetric-key change in section 7a needs no client
change.

**`DEFAULT_SERVER_URL` in `orange-copy-paste-clipboard-app-rust/src-tauri/src/sync/config.rs`
points at `https://api.example.com`.** The value is baked in at build time, so it reaches
users only in a **client release**. The self-hosted URL first shipped in client `v0.2.0`.
An install can be repointed sooner by editing `sync_server_url` in `settings.json`. The old
Render service is retired, and no endpoint ran in parallel during the switch. An install
older than `v0.2.0` stays offline until it updates. Make sure `APP_CORS_ORIGINS` includes
the app's origin (`tauri://localhost` by default).

---

## 12. Ongoing operations

**Monitoring (Netdata).** Netdata runs as the `netdata` service in the prod stack. Caddy
publishes it at `https://status.example.com` behind basic auth, and its metrics database
lives in the `netdatalib` volume. It replaced Uptime Kuma, which answered "is it up" and
nothing else. As configured here, it charts CPU, memory, disk space and IO, network, and
per-container CPU/memory/IO for every service in the stack. It samples every second, and
its stock alarms cover the things that matter.

**Host access is narrowed.** The container mounts `/proc`, `/sys`, `/etc/os-release`,
`/etc/passwd`, `/etc/group`, `/etc/localtime` and `/var/log`, read-only, and not the whole
host filesystem. It runs with `SYS_PTRACE` only, without `SYS_ADMIN` or
`apparmor:unconfined`. Two things those used to buy are expected to go: per-mount
disk-space charts for host filesystems (the old `/host/root` mount), and mapping container
network interfaces to container names.

To check what the narrowing cost:

1. After the first deploy with this change, open the dashboard. The root filesystem should
   still have a disk-space chart and alarm.
2. If it does not, and you want the disk-full alarm back, re-add
   `- /:/host/root:ro,rslave` to the `netdata` volumes in `docker-compose.prod.yml`.
   **Warning:** that mount gives the agent read access to every file on the box, `.env`
   included.

**Restrict who can reach it.** `METRICS_ALLOW_CIDR` in `.env` is a space-separated list of
source ranges that Caddy lets through to the status site. Everyone else gets 403 before
basic auth. Unset, it allows everyone. To limit it to your own address:

```bash
cd ~/app
echo 'METRICS_ALLOW_CIDR=203.0.113.7/32 2001:db8::/64' >> .env
./deploy/deploy.sh --force
```

**Basic auth is not optional.** The agent dashboard has no login of its own and reports
processes, listening ports, disk layout and container internals. Published bare, it is a free
reconnaissance page for the box. Generate the hash on the box and put it in `.env`; nobody
but you needs to see the password:

```bash
docker run --rm -it caddy:2-alpine caddy hash-password
```

It prompts, so the password never reaches your shell history. It runs a throwaway container
rather than `exec`-ing into the running one, on purpose. The compose file requires
`METRICS_AUTH_HASH`, so while it is unset **every** compose command fails, and there is no
`caddy` container to exec into. Paste the whole `$2a$...` string:

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

A blank hash fails closed: Caddy answers 401 to everyone, including you. From the outside
that looks exactly like working auth. Prove the value arrived instead of assuming it:

```bash
cd ~/app
docker compose -f docker-compose.prod.yml exec caddy printenv METRICS_AUTH_HASH
```

That must print the whole `$2a$14$...` string with single `$`. If it prints nothing, the
escaping is wrong. Then prove it end to end with **both** lines, because a blank hash also
produces a 401 on its own:

```bash
curl -s -o /dev/null -w 'no-auth %{http_code} (expect 401)
' https://status.example.com
curl -s -o /dev/null -u admin -w 'with-auth %{http_code} (expect 200)
' https://status.example.com
```

Caddy also **refuses to start** if `METRICS_AUTH_HASH` is unset entirely. That is
deliberate: a missing password should give a site that does not come up, not one that comes
up unprotected.

**What it watches beyond the machine.** The repo adds two checks to the stock config:

| Check | Where | Why it is not the default |
|---|---|---|
| `api_direct` -> `http://api:8000/internal/healthz` | `netdata/conf/go.d/httpcheck.conf` | Matches the **body** for `"status":"ok"` |
| `api_public` -> `https://api.example.com/internal/healthz` | same | Same match, through Caddy and TLS |

Neither is bind-mounted. Netdata's entrypoint copies stock config into `/etc/netdata` on
every start, so a read-only mount anywhere under that path makes the copy fail and the
container crash-loop. `deploy.sh` installs these files into the `netdataconfig` volume
instead, and restarts Netdata only when their content changed. They ship from git with the
code, with no step you have to remember.

The body match is the whole point. `/internal/healthz` returns **200 even when degraded**.
A dead Postgres or Redis shows only in the body (`src/admin/router.py`), so a status-code
check would stay green straight through a database outage. The pair of jobs localises a
fault: public failing while direct passes means Caddy, TLS or DNS. Both failing means the
app, Postgres or Redis.

**The dashboard is trimmed, on purpose.** Netdata's defaults collect everything a machine
*could* have. On a small VPS that buries the charts that matter under hardware we do not
own and kernel counters nobody will act on. `netdata/conf/netdata.conf` switches these off:

- **`apps = no`, the biggest cut by far.** apps.plugin charts every application, user and
  user group separately: 644 + 168 + 154 + 46 charts here. That was roughly 90% of what
  survived the other cuts, and it answers nothing the per-container charts do not.
  `htop` and `docker stats` cover per-process detail, and both are already on the box.
- **`netdata pulse = no`, the second.** Before v2 this key was `netdata monitoring`. It
  covers the agent's charts about *itself*: dbengine compression ratio, database pages,
  worker thread timings, query latency. That is the whole "Netdata Monitoring" menu, and
  it answers questions about the monitoring tool, not the box.
- **Per-systemd-service cgroup charts** (22 units, 7 charts each), via
  `cgroups to match as systemd services = !*`. That key governs them in Netdata v2, which
  removed the old `enable systemd services` switch. These were cgroup resource charts, not
  unit state. Unit state is a separate plugin, `systemd-units`, also switched off here (see
  the gaps below).
- **Anomaly detection.** A model per dimension costs real CPU and memory here, and
  produces a second thing to interpret, not an answer.
- **Kernel and hardware detail:** pressure stall, interrupts and softirqs, deep TCP kernel
  counters (out-of-order segments, SYN cookies, ECN), IPv6/SCTP/NFS stacks, network flows,
  debugfs, statsd, ZFS, Btrfs, software RAID, batteries, ECC, Infiniband, NUMA, entropy
  and SysV IPC.
- **Network interfaces** are filtered to the real uplink. Docker gives every compose
  network a bridge and every container a veth. Each would otherwise become a menu entry
  named after a hash. **Disks** drop loopback, ramdisk and device-mapper entries.

What is deliberately kept is the list you would want during an incident: CPU, RAM and
swap, disk space and IO, network throughput, per-container CPU/memory/IO for all five
services, and the two API health checks.

**Check a config key before trusting it.** The agent does not fail on a key it does not
recognise, but it usually **says so** in the config it serves back at `/netdata.conf`:

- An unknown key is marked `found in the config file, but is not used`.
- A renamed key is annotated `migrated from`.
- **`[plugins]` is the exception, and it is a trap.** That section takes an arbitrary
  plugin name as a key, so a misspelled plugin is accepted in silence and does nothing.
  Never write a plugin name from memory; read it off the `plugin=` field of the charts you
  want gone:

```bash
cd ~/app
docker compose -f docker-compose.prod.yml exec -T netdata curl -s 'localhost:19999/api/v1/charts' | python3 -c "import json,sys; d=json.load(sys.stdin)['charts']; s={}; [s.__setitem__((c.get('plugin'),c.get('module')), s.get((c.get('plugin'),c.get('module')),0)+1) for c in d.values()]; [print('%4d  %-22s %s' % (n,p,m)) for (p,m),n in sorted(s.items(), key=lambda x:-x[1])]"
```

Then check both the count and the served config:

```bash
cd ~/app
docker compose -f docker-compose.prod.yml exec -T netdata curl -s 'localhost:19999/api/v1/charts' | grep -o '"id":"' | wc -l
```

**The deploy reports on itself.** A pipeline that stops working is silent by nature: the
timer fires, the script fails early, the old containers keep serving, and nothing looks
wrong until someone notices a merged commit never shipped. That happened twice here. So
`deploy.sh` posts to the same Discord channel as the alarms, only when there is something
to say. The ~90s no-op polls are silent:

| When | Message |
|------|---------|
| A commit deployed | Green **Deployed `rovertools-api:<sha>`**, with the commit subjects that shipped (up to 8, then a count), the commit range, files changed, wall-clock duration, and whether Caddy reloaded and how many Netdata config files were installed |
| The range shipped a migration | The same, **amber**, with a `MIGRATIONS` field naming how many revision files arrived. A deploy never runs Alembic, so the database is now behind the code and the symptom is a live route 500ing on a missing relation |
| Any non-zero exit | Red **Deploy FAILED on `<host>`**, naming the **stage** it died in (`git fetch`, `signature check`, `git reset`, `docker build`, `container rollout`, `caddy reload`, `netdata config`), the exit code, the commit, and the `journalctl` line to run |

How the reporter is built, and why:

- **The embed JSON is built by `python3` reading environment variables,** not by pasting
  strings together in shell. Commit subjects contain quotes, backslashes and non-ASCII. A
  hand-rolled shell escaper gets one of those wrong eventually, and then the webhook
  silently rejects the post. If `python3` is ever missing, the deploy says so and carries
  on rather than dying inside its own error handler.
- **Two values cross the self-re-exec (section 9, "The files") in the environment:** the
  **pre-pull commit** and the **start time**. Without the first, the re-exec'd process
  compares HEAD against itself and reports an empty commit list. That is why the first
  notifications said nothing but the image tag.
- **The failure path is an `EXIT` trap,** so it covers every way the script can die: a
  failed `git fetch`, a broken build, a container that will not come up. It is not limited
  to the errors someone thought to handle. It does not fire on the self-re-exec, because
  `exec` replaces the process image without running traps.
- **It is deliberately independent of Netdata.** `rovertools-deploy.service` is a oneshot
  that is inactive between runs. Inferring "the pipeline is healthy" from a unit that is
  *supposed* to be idle most of the time is the kind of indirect guarantee that failed us
  twice. The thing doing the work reports on the work.
- **A broken notifier never breaks a deploy.** The webhook is read from `.env`, and a
  failed post is non-fatal.

**Notifications.** Alarms reach Discord through a **custom sender** defined in
`netdata/conf/health_alarm_notify.conf` in the repo. Two decisions worth knowing:

- **Not email.** `SEND_EMAIL="NO"`. OVH filters outbound SMTP, so any mail-based alert
  fails silently. The same filter is why the app sends through Brevo's HTTPS API (section
  13).
- **Not the stock Discord sender.** `SEND_DISCORD="NO"` and `SEND_CUSTOM="YES"`. The stock
  sender works, but its message is a wall of italic prose. The custom sender posts a
  colour-coded embed (red critical, amber warning, green recovered), with the value, chart
  and previous state as separate fields. The embed colour carries severity, so the text
  stays plain ASCII and reads on a phone. Enable only one of the two senders, or every
  alarm arrives twice.

The config is a **minimal override**. `alarm-notify.sh` sources the stock file first and
this one second, so anything not named here keeps its stock behaviour.

**The webhook is the only part not in git.** Create it in Discord (Server Settings ->
Integrations -> Webhooks -> pick a channel -> Copy Webhook URL), then:

```bash
cd ~/app
echo 'ALERT_DISCORD_WEBHOOK=<paste the webhook URL>' >> .env
./deploy/deploy.sh --force
```

`docker-compose.prod.yml` passes it to the container, and the sender reads it. `deploy.sh`
reads the same line from `.env`. The name matters: the stock notify config assigns
`DISCORD_WEBHOOK_URL=""` before ours is sourced, so reusing that name would shadow the
value with an empty string.

**Test it, do not assume it.** Netdata ships a test path that fires all three states:

```bash
cd ~/app
docker compose -f docker-compose.prod.yml exec -T netdata bash -c '/usr/libexec/netdata/plugins.d/alarm-notify.sh test'
```

Three messages should arrive: warning, critical, recovered. If none do, that command's
output says why. Read it rather than inferring success from silence.

**What reaches you, and what does not.** The whole point of the setup, in one table:

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
- **systemd unit state.** `fail2ban` or `nftables` dying is silent. `systemd-units` is its
  own plugin, not a go.d module, and `netdata.conf` switches it off. It would produce
  nothing in this container anyway: it talks to systemd over D-Bus, and `/run/systemd` is
  not mounted. Closing this gap means mounting the host's systemd socket *and* filtering to
  a few units, or every unit on the box lands back on the dashboard. That is not done. The
  units that would take the service down with them (`docker`, the API container) are
  already covered by the container and health-check alarms.

**Rotate the webhook if it has been pasted anywhere shared.** Anyone holding the URL can
post into that channel. The blast radius is spam in one channel, not access to the box, and
the fix is free:

1. Regenerate the webhook in Discord (one click).
2. Replace the `ALERT_DISCORD_WEBHOOK` line in `.env`.
3. Re-run the deploy.

**What this still cannot tell you.** Netdata runs on the box it watches. If the VPS is down
or off the network, the dashboard is down with it and no alert is sent. Only a **free
external check** (UptimeRobot, Better Stack) catches a whole-box outage. Point it at
`https://api.example.com/internal/healthz` with a keyword match on `"status":"ok"`. Run one
alongside Netdata; it is the one piece that cannot live on the box.

Two standing choices about the agent itself:

- **Retention and footprint.** Netdata's default database tiers keep roughly a day of
  per-second data and months of downsampled history, sized to what the `netdatalib` volume
  can take. On this box (3.7 GiB RAM, 38 GB disk) that is comfortable, but Netdata is the
  largest thing in the stack by memory. The collector trim above already removed most of
  the cost. If it ever crowds the API again, cut retention in `netdata/conf/netdata.conf`
  and deploy. Editing it in the container instead (`./edit-config`) puts the box out of
  step with git, the drift `netdata/conf/**` exists to prevent.
- **Netdata Cloud stays unclaimed.** `DISABLE_TELEMETRY=1` is set and no claim token is
  configured, so the agent talks to nobody. Claiming it would put the box's metrics on
  someone else's dashboard. Make that decision deliberately, not by drift.

**Inspecting logs.** Everything lands in the host's systemd journal, which is persistent
and survives the container swaps a deploy makes. There are two sources: the deploy runner
and the app containers. The deploy runner's log shows whether a poll picked up a commit
and whether the build and rollout succeeded:

```bash
journalctl -u rovertools-deploy.service -f              # live, follow (Ctrl-C to stop)
journalctl -u rovertools-deploy.service -n 100 --no-pager   # last run's output
systemctl status rovertools-deploy.timer               # poll active? last / next fire
```

The containers log to the journal through the `journald` driver, tagged per service
(`docker-compose.prod.yml`). Read them by tag. The tag is stable across rollouts, so old
and new containers appear under one name:

```bash
journalctl -t rovertools-api -f            # API, live
journalctl -t rovertools-api -n 200 --no-pager
journalctl -t rovertools-api --since '1h'  # bound the window (or --since 10m)
journalctl -t rovertools-caddy             # TLS / proxy
journalctl -t rovertools-redis
```

Because it is the journal, not ephemeral container output, `--since`/`--until` reach back
past the current container's lifetime. Other views:

- **A plain file to hand someone:** redirect any of the above, for example
  `journalctl -t rovertools-api --since today > api-$(date +%F).log`.
- **Current state, not history:** `docker compose -f docker-compose.prod.yml ps` (health,
  restarts) and `docker stats --no-stream` (per-container CPU/memory).

The journal is capped: section 5.8 sets persistent storage and a 500 MB / one-month
ceiling, so old lines age out rather than filling the disk.

The remaining routine operations:

- **Schema changes.** Author the Alembic revision and review it. Then apply it yourself
  (section 8) *before* the deploy that needs it; a green deploy is not evidence anything
  migrated. From the moment you migrate until the deploy lands, **old code runs against the
  new schema**, so never ship a breaking migration in one shot. Use expand/contract: an
  additive migration (deliberate), then the code deploy, then later a contractive migration
  (deliberate).
- **Supabase key rotation.** Rotating an asymmetric signing key needs no action; the
  backend re-fetches the JWKS, which it caches for 5 minutes. Rotating a *legacy* HS256
  secret means updating `SUPABASE_JWT_SECRET` and redeploying, which invalidates live
  sessions.
- **Scaling.** The service can run more `api` replicas; the prod stack runs one today.
  Redis pub/sub handles cross-replica fan-out. The pool bounds (24 request / 2 listener
  connections per replica, `src/redis_client.py`) stay well under a self-hosted Redis's
  limits. Redis holds only ephemeral presence and pub/sub traffic, so it stays small.
- **Patching and reboots.** unattended-upgrades patches nightly and reboots at 04:30 UTC
  when a patch needs it (section 4). The reboot briefly drops WebSockets, and clients
  reconnect. Docker is patched the same way, from Ubuntu packages.
- **Supabase and R2 stay external.** The move onto the VPS did not touch them, and the
  `DATABASE_URL` secret on the migrate workflow still works.

**Costs.** A fixed VPS cost, plus the external tiers:

| Service | Free tier | Beyond it |
|---------|-----------|-----------|
| Supabase | 500 MB Postgres + 5 GB egress; **pauses after ~1 week idle**. Text entries are tiny, so the DB is rarely the wall | **Supabase Pro (~$25/mo)** for always-on plus headroom: the meaningful first bill |
| R2 | 10 GB storage with **zero egress**. Binary blobs are the real storage cost; at the 50 MB default quota the free 10 GB covers ~200 users | ~$0.015/GB-mo |
| Redis | In-container and free | - |

`DEFAULT_BLOB_QUOTA_BYTES` in `.env` sets the default per-user blob quota. The quota and
per-entry limits themselves are in [architecture.md](architecture.md).

---

## 13. Troubleshooting

**Every authenticated request returns 401.** Almost always a JWT mismatch. Decode the token
(jwt.io) and read the `alg` header:

- `alg: ES256`/`RS256` -> `SUPABASE_URL` must be set and correct; the backend derives
  `<SUPABASE_URL>/auth/v1/.well-known/jwks.json` from it.
- `alg: HS256` -> `SUPABASE_JWT_SECRET` must be set and match the project. With no secret
  set, the backend answers every HS256 token with 401.

Also confirm the `aud` claim is `authenticated` (`SUPABASE_JWT_AUDIENCE`).

**500 on an authenticated request, naming a missing setting.**

- `500 "Received an asymmetric token but SUPABASE_URL is not configured"`: an asymmetric
  token arrived, but no project URL is set.

**`OSError: [Errno 101] Network is unreachable` on every DB call.**

- **Symptom:** the service starts fine, logs `maintenance loop error; retrying` in a loop,
  and fails its health check.
- **Cause:** `DATABASE_URL` points at the direct `db.<ref>.supabase.co` host (IPv6-only),
  and v6 routing is failing.
- **Fix:** switch `DATABASE_URL` to the Supavisor pooler (section 7a). The VPS has IPv6,
  but the pooler removes the dependency on it.

**`prepared statement "__asyncpg_..." does not exist`.**

- **Cause:** you are on the transaction pooler (port 6543) with statement caching on.
  `src/database.py` disables caching for URLs containing `:6543`. If you see this error,
  the port is not literally in the URL.
- **Fix:** switch to session mode (5432).

**Health check fails.** `/internal/healthz` touches Postgres and Redis. Check:

- `DATABASE_URL`: pooler host? asyncpg driver? password URL-encoded?
- that the `redis` container is up (`docker compose -f docker-compose.prod.yml ps`).

**WebSocket connects then drops.** A single drop right after a deploy or the 04:30 reboot
is expected; clients reconnect. For persistent drops, confirm:

- the client sends the token in its first message, the `auth` message, within the
  server's time limit ([architecture.md](architecture.md) has the handshake);
- Caddy is proxying `/ws` (it does by default).

**Invite emails never arrive.** Delivery is best-effort. `send_sharing_invite` logs the
failure and swallows it, so the inviter's request still succeeds. From the client, a broken
mail config looks exactly like a working one. Ask the deployment what it thinks it is
doing. Caddy answers 404 for `/internal/*` from the internet, so `<base>` must be an
address reached from inside the box (section 10 shows one way):

```bash
curl -s -H "X-Admin-Key: $ADMIN_API_KEY" <base>/internal/v1/admin/email
curl -s -X POST -H "X-Admin-Key: $ADMIN_API_KEY" -H 'Content-Type: application/json' -d '{"to":"you@example.com"}' <base>/internal/v1/admin/email/test
```

The first reports the provider and whether its credentials are present, without echoing
any secret. The second sends one message and returns the actual error. Two causes cover
nearly every case:

- `provider: brevo, brevo_api_key_set: false` - `BREVO_API_KEY` is missing from the `.env`.
  Sends raise before touching the network.
- `provider: smtp` and nothing arrives - OVH filters outbound SMTP. Port 25 is blocked, and
  submission ports can be too. Prefer `EMAIL_PROVIDER=brevo`, which sends over HTTPS and is
  not affected. Account emails (verification, password reset) come from Supabase and are
  unaffected either way.

**Account mail looks nothing like the invite.**

- **Cause:** Supabase renders its own templates from its dashboard, and by default these
  are its stock ones.
- **Fix:** `docs/supabase-email/` holds copies built from this repo's mail shell. Paste
  them into Authentication -> Emails -> Templates, subjects included. Re-run
  `uv run python scripts/render_supabase_emails.py` after any change to the shell.
- **Also:** the built-in Supabase mailer is rate limited to a couple of messages an hour.
  Configuring custom SMTP is what makes signup and reset mail dependable.

**Blob upload fails, everything else works.**

- **Cause:** R2 misconfiguration, or a badly skewed client clock. Presigned PUTs expire in
  5 minutes and GETs in 1 hour, so clock skew also breaks uploads.
- **Fix:** verify the bucket exists, `AWS_REGION=auto`, and the endpoint is the
  account-level R2 URL.

**A Caddyfile change does not take effect, and `caddy reload` says "config is unchanged".**

- **Symptom:** the file on disk clearly has your change, `caddy validate` passes, and the
  reload logs `"config is unchanged"`.
- **Cause:** the container is reading a stale copy. A *single-file* bind mount binds the
  inode. `git pull` replaces the file rather than editing it in place, so the container
  keeps the inode it started with. `validate` and `reload` inside the container then both
  operate on the old file.
- **Fix:** already in place. The `caddy/` **directory** is mounted instead
  (`./caddy:/etc/caddy:ro`), which resolves the path fresh, and `deploy.sh` has a reload
  step. On a container predating that fix, run
  `docker compose -f docker-compose.prod.yml up -d --force-recreate caddy`.

---

## 14. Decisions - settled and open

**Settled 2026-08-24:**

- **Host** - self-hosted OVH VPS-1, replacing Render. Fixed monthly cost, full control, and
  Redis co-located so it can bind to an internal network with no tunnel or TLS.
- **Ingress/domain** - a free FreeDNS name, `api.example.com` -> `203.0.113.10`, with
  Caddy owning TLS through Let's Encrypt. Chosen over an owned domain (~$10/yr; swap later
  by changing one Caddyfile hostname + `PUBLIC_BASE_URL`). Also chosen over a Cloudflare
  Tunnel, which has zero inbound ports but adds a hop in the WebSocket path and a daemon to
  keep up. The temporary name brought the pipeline up for free.
- **Deploy stack** - Docker Compose + Caddy, on a systemd poll. The draw is tagged images and
  one-step rollback.
- **Deploy rollover** - plain `docker compose up -d` recreate, **accepting a ~1-3s 502 window
  per deploy**, over an overlap tool. `docker-rollout` was declined on trust (a third-party
  script with Docker/root access), and Swarm as cluster-weight on one host. A Caddy
  `lb_try_duration` retry was tried and measured not to help: one container means an empty
  pool, with nothing to fail over to. Infrequent deploys and retrying clients make the gap
  cheap. See "What zero downtime means here".
- **Build + delivery** - **build on the box, polled from git**, over GitHub Actions +
  GHCR. The cost is set out at the top of section 9. A `git` poll also needs no inbound
  endpoint, no webhook secret and no CI credentials on the box. Cost was the deciding
  factor (the user's call). The trade is a ~90s deploy latency and the VPS doing the build.
  Rejected alternatives:
  - GitHub Actions + GHCR push: the cost we are avoiding.
  - A self-hosted Actions runner: unlimited minutes, but still a runner daemon and the
    Actions dependency.
  - A webhook receiver: instant, but an inbound endpoint and an HMAC to secure.
- **Deploy identity** - runs as the `ubuntu` login user (already in `docker`) with an
  outbound read-only token (section 5.6). Chosen over an inbound forced-command CI key and
  over a separate no-SSH service account. Polling removed any inbound path, so a dedicated
  user bought little. This is strictly less surface and less friction.
- **Redis** - a compose service, not a host package, so it is versioned and torn
  down/rebuilt with the stack.

**Open / to do:**

- Retire the old push-deploy `deploy` user if the box still carries it (section 9, "One-time
  box wiring", has the commands and the warning).
- Move off the temp FreeDNS name to a permanent domain when ready (Caddyfile + `PUBLIC_BASE_URL`).
- **Add a free external uptime check** on the public healthz with a `"status":"ok"` keyword
  match. Nothing on the box can report the box being down.

---

## Local Development

For a full local stack (Postgres, Redis, and MinIO standing in for R2), see the
`docker-compose.yml` at the repo root and the setup notes in the main `README`. The dev
stack has five services, and every published port binds to loopback only:

- `api` - FastAPI `uvicorn --reload` on `:8000`.
- `db` - `postgres:16-alpine`, the local stand-in for Supabase Postgres.
- `redis` - `redis:7-alpine`.
- `minio` - `minio/minio`, the local stand-in for R2, with its console on `:9001`.
- `createbuckets` - a one-shot `minio/mc` job that creates the `clipboard-blobs` bucket.

Blobs use MinIO or a real R2 bucket, set through env. You still need a real Supabase
project locally, because the backend verifies Supabase-issued JWTs and never signs its own.

Related: [architecture.md](architecture.md) for the wire contract, and
`docs/permissions.md` at the workspace root for who may do what.
