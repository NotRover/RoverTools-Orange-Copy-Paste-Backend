# Orange Clipboard — Backend Deployment

**Owns:** the self-hosted OVH VPS that runs the sync backend and Redis — how to reach it,
recover it, harden it, and deploy the API onto it (Docker Compose + Caddy + CI/CD). Also
the external services the backend depends on (Supabase, R2) and the migration discipline.
**Not here:** the wire contract — routes, payloads, DDL, socket events, crypto envelope —
which is [ARCHITECTURE.md](ARCHITECTURE.md); client internals (the app's own
`docs/ARCHITECTURE.md`); who-may-do-what (`docs/PERMISSIONS.md` at the workspace root).

**No secrets live in this file.** Passwords and keys are named by *where they live*, never
by value. Host-key fingerprints and the public IP are safe to record — fingerprints are
public by design and the IP is not a secret. Anything pasted here is in git history
forever; if that ever happens, rotate it, do not edit it out.

> The box hosts the backend, so its deployment runbook is here in the backend repo. Both
> the backend and the parent workspace are private repos (verified 2026-08-24). The
> Render/Supabase/R2 hosting this replaces was retired 2026-08-24; this doc is the single
> home for how the backend runs now.

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
9. [Deployment — Docker Compose + Caddy + CI/CD](#9-deployment--docker-compose--caddy--cicd)
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
| Domain | `rovertools-temp.ctx.cl` (FreeDNS, temporary/shared) -> `203.0.113.10` |
| OS | Ubuntu 26.04 LTS (resolute) |
| Size | 2 vCPU - 3.7 GiB RAM - 38 GB disk |
| Timezone | UTC |
| Admin user | `ubuntu` (passwordless `sudo`) |
| `root` | locked — no root login by any path, including the console |
| Deploy user | `deploy` (no sudo, in `docker` group, key locked to one command) |

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
  account buys little; the box reaches **out** to GitHub with an **outbound, read-only** repo
  deploy key (`~/.ssh/id_repo`), and nothing reaches in. `.env` lives inside the checkout,
  git-ignored, mode 600 (a `git reset --hard` leaves ignored files alone).
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
--all`. A scanner (195.178.110.30) was banned within seconds of starting the jail — that is
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

### 5.6 Read-only deploy key

The deploy runs as `ubuntu` (section 4), so the outbound key that `git fetch` uses lives in
`ubuntu`'s home. It is **read-only** — the box only ever pulls — and never leaves the box.

```bash
ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_repo -C 'rovertools-box-readonly'
cat ~/.ssh/id_repo.pub
#   -> add that PUBLIC key as a READ-ONLY Deploy key on the GitHub repo.
```

The rest of the wiring — cloning the repo, `.env`, the optional `docker-rollout` plugin, and
the systemd timer — is in section 9, since it depends on the repo files.

**Historical:** an earlier design added a separate `deploy` user with an inbound,
forced-command-locked CI key (`id_ci`) for a GitHub Actions push-deploy. The poll model
dropped it. On a box provisioned under the old design, remove the whole account:
`sudo userdel -r deploy`, drop `deploy` from `AllowUsers`, `sudo rm -rf /opt/rovertools`.

### 5.7 Domain (FreeDNS)

The temp/free name for Caddy's TLS. At `freedns.afraid.org`, add an **A** record for a
subdomain pointing at the box's IPv4. We used `rovertools-temp.ctx.cl` -> `203.0.113.10`.

**Verify:** `nslookup rovertools-temp.ctx.cl 8.8.8.8` returns `203.0.113.10`.

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
                   ▲  git fetch (read-only deploy key, outbound)
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
- Config travels with the code: `docker-compose.prod.yml`, `Caddyfile`, and `deploy.sh` all
  live in the checkout, so a change to any of them ships on the next poll like app code does.

### What "zero downtime" means here

There is one `api` container, and a deploy recreates it — so there is a ~3s gap with no
backend. We do **not** run an overlap tool (docker-rollout/Swarm); instead Caddy absorbs the
gap, which splits the story in two:

- **HTTP** — no errors. Caddy is set to hold a request and retry the upstream for up to 10s,
  every 250ms (`lb_try_duration` / `lb_try_interval` in the `Caddyfile`), re-resolving `api`
  through Docker DNS each interval. A request that lands mid-swap arrives a couple seconds
  late instead of 502ing; retrying a dial that never connected is safe for any method. This is
  "no failed requests", not literally zero-latency — the accepted trade for keeping the stack
  a plain `docker compose up -d` with nothing third-party running as root.
- **WebSockets** — the old container's open sockets drop once, when it is removed, and clients
  reconnect (a new handshake mid-gap is retried like any HTTP request). The presence heartbeat
  (`PING_INTERVAL = 25s`, `src/realtime.py`) keeps a socket under any 100s idle timeout and
  re-`SET`s presence on reconnect.

**Why not docker-rollout / Swarm.** True zero-gap needs two `api` containers overlapping.
`docker-rollout` is a third-party single-file script run with Docker (root) access — declined
on trust grounds; Swarm is a cluster orchestrator whose weight is hard to justify on one host.
For a single VPS with infrequent deploys, the Caddy retry covers the case that matters (no
failed HTTP) at zero added surface. `deploy.sh` always does a plain `docker compose up -d` —
an earlier version guarded a `docker rollout` branch, but the guard misfired once the plugin
was removed (`docker <unknown> --help` exits 0, so the branch ran and broke the deploy), so
the branch is gone. To use overlap later, reintroduce it deliberately.

### Architecture

```
                        VPS  (rovertools-temp.ctx.cl)
   internet --> :443 --> Caddy --> api  xN  --> Supabase (Postgres + Auth)  [external]
                         (TLS,       |  \------> Cloudflare R2 (blobs)       [external]
                          WS proxy,  |
                          LB)        \--------> redis   (pub/sub + presence) [in-compose]
```

- **Caddy** — the only container with published ports (80/443). Automatic TLS, proxies
  WebSockets with no config, load-balances across `api` replicas. Never rolled.
- **api** — built locally from the `Dockerfile`; **no** host port (only Caddy reaches it over
  the compose network, which is also what lets two run at once during a swap).
- **redis** — internal network, **no published port**, `requirepass`, persistence off. Not
  recreated on an `api` deploy, so presence is not needlessly flushed.

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
- **`Caddyfile`** — `rovertools-temp.ctx.cl`, auto TLS. The `dynamic a` upstream (via Docker
  DNS `127.0.0.11`) re-resolves `api` on each request so it always finds the current
  container. `lb_try_duration 10s` / `lb_try_interval 250ms` make Caddy hold and retry across
  the deploy recreate gap instead of 502ing — the reason no overlap tool is needed for HTTP
  ("What zero downtime means here").
- **`deploy/deploy.sh`** — the poll+build+deploy script (run from the checkout by the timer).
- **`deploy/rovertools-deploy.{service,timer}`** — the systemd units that poll ~every 90s.

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
little here and adds friction. The box reaches **out** to GitHub with a read-only deploy key;
nothing reaches in.

```bash
# 1. A read-only deploy key so the box can pull the private repo (outbound):
ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_repo -C 'rovertools-box-readonly'
cat ~/.ssh/id_repo.pub
#   -> add that PUBLIC key in GitHub: repo -> Settings -> Deploy keys -> Add (read-only, NO write).

# 2. Clone the repo into ~/app with that key:
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_repo -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new' \
  git clone git@github.com:Spectrewolf8/RoverTools-Smart-Clipboard-App-Backend.git ~/app

# 3. Secrets — .env lives INSIDE the checkout (git-ignored, so `git reset --hard` keeps it):
install -m 600 /dev/null ~/app/.env    # then fill it (see Secrets below)

# 4. (No docker-rollout.) We deliberately do NOT install it — Caddy's retry absorbs the
#    recreate gap instead ("What zero downtime means here"). If you ever want true container
#    overlap, that is a deliberate change: vendor a reviewed tag from
#    github.com/Wowu/docker-rollout/releases AND add the rollout branch back to deploy.sh.

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
- **`PUBLIC_BASE_URL=https://rovertools-temp.ctx.cl`** — the base of every user-facing link
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
curl -i https://rovertools-temp.ctx.cl/internal/healthz
```

Check, in order:

- **`/internal/healthz`** returns 200 with Postgres and Redis both healthy; TLS cert valid.
- An **`X-API-Version`** header is present on every response.
- **`/api/docs`** returns 404 unless `DOCS_ENABLED=true`. Leave it off in production; the
  schema maps the admin surface as well as the client one.
- **Auth works end to end** — sign in via Supabase, then call an authenticated route with
  `Authorization: Bearer <jwt>` and `X-Device-Id: <id>`. A 401 here almost always means a
  JWT config mismatch (section 13).
- **WebSocket connects and stays open**: `wss://rovertools-temp.ctx.cl/ws?token=<jwt>&device_id=<id>`.
- If `ADMIN_API_KEY` is set, `/internal/metrics` with `X-Admin-Key` returns 200 (503 means
  the key is unset).

When the pipeline is live, also run the drills: **rollout** (push a trivial change, watch
`docker rollout` health-gate the new container and drop the old — HTTP never errors, the
client reconnects once); **rollback** (redeploy the previous SHA); **reboot** (`sudo reboot`,
confirm the stack comes back on its own via `restart: unless-stopped`).

---

## 11. Point the desktop app at it

The client holds all key material and does all encryption; the server only ever sees
ciphertext. It needs the API base URL and Supabase credentials for sign-in. The client
authenticates against Supabase directly and forwards the access token as an opaque string —
it never inspects the JWT, so the asymmetric-key change in section 7a needs no client change.

**`DEFAULT_SERVER_URL` in `orange-copy-paste-clipboard-app-rust/src-tauri/src/sync/config.rs`
now points at `https://rovertools-temp.ctx.cl`** — repointed off Render in source, but baked in
at build time, so it reaches users only in a **client release** (none shipped yet). An existing
install can be repointed sooner by editing `sync_server_url` in `settings.json`. The old Render
service is retired, so there is no endpoint running in parallel during the switch — anyone who
has not updated is offline until they do. Make sure `APP_CORS_ORIGINS` includes the app's origin
(`tauri://localhost` by default).

---

## 12. Ongoing operations

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

**Costs.** Fixed VPS cost, plus external tiers: R2's free tier (10 GB, zero egress) covers
roughly 200 users at the 50 MB default quota; Supabase free works to start (expect ~$25/mo
for Pro when you want no cold-database pauses). Redis is in-container and free.

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

**Two builds of one commit differ.** Fixed once the `Dockerfile` builds `--frozen` from
`uv.lock` (section 9). Until then, the image installs with `uv pip install -e .` resolved
from `pyproject.toml`, so `uv.lock` does not pin the deployed image.

---

## 14. Decisions — settled and open

**Settled 2026-08-24:**

- **Host** — self-hosted OVH VPS-1, replacing Render. Fixed monthly cost, full control, and
  Redis co-located so it can bind to an internal network with no tunnel or TLS.
- **Ingress/domain** — a free FreeDNS name, `rovertools-temp.ctx.cl` -> `203.0.113.10`,
  Caddy owning TLS via Let's Encrypt. Chosen over: an owned domain (~$10/yr — swap later by
  changing one Caddyfile hostname + `PUBLIC_BASE_URL`), and a Cloudflare Tunnel (zero
  inbound ports but another hop in the WebSocket path and a daemon to keep up). Temp name
  first to bring the pipeline up for free.
- **Deploy stack** — Docker Compose + Caddy, on a systemd poll. The draw is tagged images and
  one-step rollback.
- **Deploy rollover** — plain `docker compose up -d` recreate, with **Caddy retrying across
  the ~3s gap** (`lb_try_duration`), over an overlap tool. `docker-rollout` was declined on
  trust (a third-party script with Docker/root access); Docker Swarm was declined as
  cluster-weight on one host. The trade: a few requests take a couple seconds longer during a
  deploy, versus zero failed requests. See "What zero downtime means here".
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
  `https://rovertools-temp.ctx.cl` in source, but it reaches users only in a client release,
  which has not shipped yet.
- Retire the old push-deploy `deploy` user if the box still carries it: `sudo userdel -r
  deploy`, drop `deploy` from `AllowUsers`, `sudo rm -rf /opt/rovertools`.
- Move off the temp FreeDNS name to a permanent domain when ready (Caddyfile + `PUBLIC_BASE_URL`).

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
  `rovertools-temp.ctx.cl` (FreeDNS) at the box. A `deploy` service account was created here
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
  `https://rovertools-temp.ctx.cl/internal/healthz` returns 200 with `db` and `redis` ok, valid
  TLS, `via: 1.1 Caddy`. Backend is live; client cutover still pending (section 11).
- **2026-08-24 — deploy rollover settled on Caddy retry, not docker-rollout.** Weighed
  docker-rollout (declined: third-party script with Docker/root access) and Docker Swarm
  (declined: cluster-weight on one host). Chose plain `docker compose up -d` with Caddy holding
  and retrying HTTP across the ~3s recreate gap (`lb_try_duration`), confirmed against Caddy's
  docs that dynamic upstreams are re-queried every retry iteration and dial failures are always
  retried. Trade: a few requests run a couple seconds late per deploy, no failed requests.
  Verify on the box with `watch -n1 curl ... /internal/healthz` during `deploy.sh --force`.

---

## Local Development

For a full local stack — Postgres, Redis, and MinIO standing in for R2 — see the
`docker-compose.yml` at the repo root and the setup notes in the main `README`. You still
need a real Supabase project locally, because the backend verifies Supabase-issued JWTs and
never signs its own.
