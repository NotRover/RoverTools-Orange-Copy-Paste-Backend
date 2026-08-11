# Orange Clipboard — Backend Deployment

Production runbook for the sync backend. For *why* the system is shaped this way,
see [ARCHITECTURE.md](ARCHITECTURE.md) (§11 Deployment Model & Cost).

## Table of Contents

1. [What You're Deploying](#1-what-youre-deploying)
2. [Prerequisites](#2-prerequisites)
3. [Provision Supabase](#3-provision-supabase)
4. [Provision Cloudflare R2](#4-provision-cloudflare-r2)
5. [Apply Database Migrations](#5-apply-database-migrations)
6. [Deploy to Render](#6-deploy-to-render)
7. [Verify the Deployment](#7-verify-the-deployment)
8. [Point the Desktop App at It](#8-point-the-desktop-app-at-it)
9. [Ongoing Operations](#9-ongoing-operations)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. What You're Deploying

Four moving parts. Only the first is code you ship:

| Component | Provider | Purpose |
|---|---|---|
| **FastAPI service** | Render (web service, Docker) | The API + `/ws` realtime endpoint |
| **Postgres + Auth** | Supabase | Ciphertext store; issues the JWTs we verify |
| **Key Value (Valkey)** | Render | WebSocket pub/sub fan-out + device presence |
| **Blob storage** | Cloudflare R2 | Encrypted image/file blobs via presigned URLs |

Redis is **not optional** — realtime fan-out and presence depend on it.
[`render.yaml`](../render.yaml) provisions it alongside the API, so there's no
separate Upstash account to manage.

The service is **stateless**: scale it to N replicas freely. Cross-replica
delivery goes through Redis pub/sub, so a client connected to replica A still
receives events published by replica B.

---

## 2. Prerequisites

**Accounts:** Supabase, Cloudflare (R2), Render — with Render connected to this
repository.

> This backend is its own repo (a submodule of the RoverTools workspace). Point
> Render at **the backend repo**, not the parent.

**Local tooling** (only needed to run migrations and verification):

- Python **3.14+**
- [`uv`](https://docs.astral.sh/uv/)

---

## 3. Provision Supabase

Create a project, then collect four values.

> **Dashboard note:** Supabase reorganised these screens in 2025. API keys now
> live under **Settings → API Keys** (also in the **Connect** dialog), and JWT
> configuration under **Settings → JWT Keys**. Older guides pointing at
> "Settings → API → JWT Secret" are stale.

| Value | Where | Env var |
|---|---|---|
| Connection string (URI) | Settings → Database | `DATABASE_URL` |
| Project URL | Settings → API Keys | `SUPABASE_URL` |
| Secret key (`sb_secret_…`) | Settings → API Keys | `SUPABASE_SERVICE_ROLE_KEY` |
| Legacy JWT secret | Settings → JWT Keys | `SUPABASE_JWT_SECRET` *(usually blank — see below)* |

### ⚠️ Use the connection pooler, not the direct host

Supabase's direct host (`db.<ref>.supabase.co`) resolves to **IPv6 only**, and
Render has no outbound IPv6. A direct URL fails at startup with:

```
OSError: [Errno 101] Network is unreachable
```

Take the **Supavisor pooler** URI instead (Connect dialog → Session pooler) and
rewrite the driver to asyncpg. Note the username carries the project ref:

```
postgresql+asyncpg://postgres.<project-ref>:<password>@aws-<region>.pooler.supabase.com:5432/postgres
```

| Mode | Port | Notes |
|---|---|---|
| **Session** (recommended here) | 5432 | Behaves like a normal connection; the app already pools |
| Transaction | 6543 | Scales to more clients; **no prepared statements** |

Both are IPv4-reachable on every tier. Session mode is the better fit — the app
maintains its own SQLAlchemy pool. If you do use transaction mode, `database.py`
detects port `6543` and disables asyncpg's statement caches automatically;
without that you'd hit `prepared statement does not exist` under load.

The direct host still works from a machine with IPv6 (e.g. running migrations
locally), and Supabase sells an IPv4 add-on if you specifically need it.

**About the JWT secret — this is the part that trips people up.** Supabase has
signed access tokens with **asymmetric keys (ES256) by default since
2025-10-01**. The backend detects the algorithm per token:

- **New project (2025-10-01 or later)** → leave `SUPABASE_JWT_SECRET` **blank**.
  Tokens are verified against the project's JWKS endpoint, derived from
  `SUPABASE_URL`. Key rotation is picked up automatically, no redeploy.
- **Older project still signing HS256** → set `SUPABASE_JWT_SECRET` to the legacy
  secret.
- **Mid-migration** → both work simultaneously; the JWKS carries the legacy
  secret alongside the new key.

`SUPABASE_SERVICE_ROLE_KEY` accepts either a new secret key (`sb_secret_…`) or the
legacy `service_role` key; Supabase deprecates the legacy keys at the **end of
2026**, so prefer a secret key. It is server-only — never ship it to a client.

---

## 4. Provision Cloudflare R2

1. Create a bucket (e.g. `clipboard-blobs`) → `S3_BUCKET`.
2. Create an R2 API token → `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY`.
3. Copy the account endpoint → `S3_ENDPOINT_URL`
   (`https://<account-id>.r2.cloudflarestorage.com`).
4. `AWS_REGION=auto`.

The bucket **must already exist** — the service only presigns URLs, it never
creates buckets. Text and note sync work without R2; only large image/file
attachments need it.

---

## 5. Apply Database Migrations

**Migrations are never applied automatically.** Nothing in the app's startup path
runs them, and `render.yaml` ships with `preDeployCommand` commented out
deliberately. Applying schema changes is an explicit, reviewed action.

Run them from your machine against Supabase, before the first deploy:

```bash
uv sync
DATABASE_URL="postgresql+asyncpg://postgres:<pw>@db.<ref>.supabase.co:5432/postgres" uv run alembic upgrade head
```

`migrations/env.py` reads `DATABASE_URL` from the environment, overriding
`alembic.ini`. Confirm where you're pointed before running it — this writes to a
real database.

To let Render migrate on every deploy instead, uncomment `preDeployCommand` in
`render.yaml`. Only do that if you accept schema changes landing automatically.

---

## 6. Deploy to Render

[`render.yaml`](../render.yaml) is a Blueprint describing both services, so the
deploy is reproducible rather than hand-clicked.

1. **Render Dashboard → New → Blueprint**, select this repository.
2. Render reads `render.yaml` and shows the two services:
   `rovertools-clipboard-api` (web) and `rovertools-clipboard-keyvalue` (Valkey).
3. You'll be prompted for every `sync: false` variable — paste the values from
   §3 and §4. Leave blank any you're not using (`SUPABASE_JWT_SECRET` on a new
   project, `BREVO_API_KEY` if you don't use sharing invites, `ADMIN_API_KEY` to
   keep the admin surface disabled).
4. Apply.

`REDIS_URL` is wired automatically from the Key Value instance over Render's
private network — don't set it manually.

### Choices baked into the Blueprint

- **`plan: starter`, not `free`.** Free web services idle out after inactivity,
  and an idled instance cannot hold WebSocket connections — realtime sync would
  silently stop. Free is fine for a one-off smoke test.
- **`healthCheckPath: /internal/healthz`** — public, unauthenticated, and it
  checks Postgres *and* Redis, so a broken dependency fails the deploy loudly.
- **`region: oregon`** — change it to sit near your Supabase region; every
  request makes a database round trip.
- **`ipAllowList: []`** on Key Value — private-network access only.
- **WebSockets need no extra configuration.** Render routes all traffic,
  including upgrades, to the service port.

### About the port

Render assigns `$PORT` (default 10000). The `Dockerfile` binds
`0.0.0.0:${PORT:-8000}`, so it works on Render and under plain `docker run`
without changes. Don't hardcode a port in a start command.

---

## 7. Verify the Deployment

```bash
curl -i https://<your-service>.onrender.com/internal/healthz
```

Check, in order:

- **`/internal/healthz`** returns 200 with Postgres and Redis both healthy.
- An **`X-API-Version`** header is present on every response.
- **`/api/docs`** loads the Swagger UI.
- **Auth works end to end** — sign in via Supabase, then call an authenticated
  route with `Authorization: Bearer <jwt>` and `X-Device-Id: <id>`. A 401 here
  almost always means a JWT config mismatch (§10).
- **WebSocket connects and stays open**:
  `wss://<service>/ws?token=<jwt>&device_id=<id>`.
- If `ADMIN_API_KEY` is set, `/internal/metrics` with `X-Admin-Key` returns 200
  (503 means the key is unset).

---

## 8. Point the Desktop App at It

The client holds all key material and does all encryption; the server only ever
sees ciphertext. It needs two things: the API base URL, and Supabase credentials
for sign-in.

The client authenticates against Supabase directly and forwards the resulting
access token as an opaque string — it never inspects or verifies the JWT. **The
asymmetric-key change in §3 therefore requires no client change.**

Make sure `APP_CORS_ORIGINS` includes the app's origin (`tauri://localhost` by
default).

---

## 9. Ongoing Operations

**Schema changes.** Author the Alembic revision, review it, then apply it
explicitly (§5). Deploy the code *after* the migration when the change is
additive; for destructive changes, plan an expand/contract sequence so the
running replicas tolerate both shapes.

**Supabase key rotation.** Rotating an asymmetric signing key needs no action —
the JWKS is re-fetched (cached ~5 minutes). Rotating a *legacy* HS256 secret
means updating `SUPABASE_JWT_SECRET` and redeploying, which will invalidate live
sessions.

**Scaling.** Increase replica count freely; Redis pub/sub handles cross-replica
fan-out. Redis holds only ephemeral presence and pub/sub traffic, so it stays
small.

**Costs.** R2's free tier (10 GB, zero egress) covers roughly 200 users at the
50 MB default quota. Supabase free works to start; expect ~$25/mo for Pro when
you want no cold-database pauses. Redis stays well inside any small plan.

---

## 10. Troubleshooting

**Every authenticated request returns 401.** Almost always a JWT mismatch.
Decode the token (jwt.io) and read the `alg` header:

- `alg: ES256`/`RS256` → `SUPABASE_URL` must be set and correct; the backend
  derives `<SUPABASE_URL>/auth/v1/.well-known/jwks.json` from it.
- `alg: HS256` → `SUPABASE_JWT_SECRET` must be set and match the project.

Also confirm the `aud` claim is `authenticated` (`SUPABASE_JWT_AUDIENCE`).

**500 "SUPABASE_URL is not configured"** — an asymmetric token arrived but no
project URL is set. **500 "…SUPABASE_JWT_SECRET is not configured"** — an HS256
token arrived on a deployment configured only for asymmetric keys.

**`OSError: [Errno 101] Network is unreachable` on every DB call.** You're using
the direct `db.<ref>.supabase.co` host, which is IPv6-only, from a platform with
no outbound IPv6. Switch `DATABASE_URL` to the Supavisor pooler (§3). The symptom
is a service that starts fine, logs `maintenance loop error; retrying` in a loop,
and fails its health check — the API is up but every request touching Postgres
fails.

**`prepared statement "__asyncpg_…" does not exist`.** You're on the transaction
pooler (port 6543) with statement caching on. `database.py` disables it
automatically for `:6543` URLs — if you see this, the port isn't literally in the
URL, so switch to session mode (5432) instead.

**Health check fails on deploy.** `/internal/healthz` touches Postgres and Redis.
Check `DATABASE_URL` (pooler host? asyncpg driver? password URL-encoded?) and
that the Key Value instance provisioned.

**WebSocket connects then drops.** Usually a free-plan instance idling out
(§6). Confirm the token is passed as the `token` query parameter, since browsers
and Tauri can't set headers on a WebSocket handshake.

**Blob upload fails, everything else works.** R2 misconfiguration. Verify the
bucket exists, `AWS_REGION=auto`, and the endpoint is the account-level R2 URL.
Presigned PUTs expire in 5 minutes and GETs in 1 hour, so a badly skewed client
clock also breaks uploads.

**Reproducibility caveat.** The `Dockerfile` installs with `uv pip install -e .`
resolved from `pyproject.toml`, so `uv.lock` does **not** pin the deployed image.
Two builds of the same commit can resolve different transitive versions. If you
need byte-reproducible deploys, switch the image to install from the lockfile.

---

## Local Development

For a full local stack — Postgres, Redis, and MinIO standing in for R2 — see the
`docker-compose.yml` at the repo root and the setup notes in the main `README`.
You still need a real Supabase project locally, because the backend verifies
Supabase-issued JWTs and never signs its own.
