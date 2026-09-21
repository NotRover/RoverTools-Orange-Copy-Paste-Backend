# RoverTools' Orange Copy Paste — Backend

The cloud-sync API behind the [Orange Copy Paste desktop app](../orange-copy-paste-clipboard-app-rust): a FastAPI service that stores encrypted clipboard entries and notes, fans changes out to a user's devices in real time, and brokers sharing between users.

It is a **stateless relay and store**. All encryption happens on the client, so the server holds ciphertext, public keys, and opaque wrapped keys — never plaintext, and never a key it could decrypt with. It also never issues identity tokens: Supabase Auth signs them, this service only verifies them.

- **Runtime:** Python 3.14+, FastAPI + uvicorn, managed with `uv`
- **Data:** Supabase Postgres via SQLAlchemy async + asyncpg, migrations with Alembic
- **Realtime:** WebSocket fan-out over Redis pub/sub
- **Blobs:** S3-compatible object storage (Cloudflare R2 in production, MinIO in dev)
- **Auth:** Supabase Auth (GoTrue) JWTs, verified with PyJWT

---

## Table of contents

- [Architecture at a glance](#architecture-at-a-glance)
- [Getting started](#getting-started)
- [Configuration](#configuration)
- [API surface](#api-surface)
- [The client contract](#the-client-contract)
- [Project structure](#project-structure)
- [Development](#development)
- [Deployment](#deployment)
- [Further reading](#further-reading)

---

## Architecture at a glance

```
 Desktop client ──HTTPS──►  FastAPI  ──►  Postgres     (ciphertext, keys, membership)
       │                       │
       │                       ├────►     Redis        (pub/sub fan-out, device presence)
       └────WSS /ws────────────┤
                               └────►     S3 / R2      (presigned blob upload & download)
```

- **Auth is delegated.** The client authenticates against Supabase directly and attaches the resulting access token to every call here. This service verifies it — asymmetric ES256/RS256 against the project's JWKS, or legacy HS256 for older projects — and reads `sub` as the user id. There is no login, refresh, or password route on this server.
- **Sync is last-write-wins.** Entries are keyed by `(user_id, client_id, entry_type)` and resolved on `updated_at`. Deletes are tombstones — a push carrying `deleted_at` — so there is deliberately no delete route, and a tombstone always wins a conflict.
- **Realtime is horizontal.** Each process holds its own WebSocket connections and subscribes to Redis (`user:*`, `space:*`). Any process can publish, so every replica delivers, and the API scales out behind a load balancer without sticky sessions.
- **Blobs bypass the API.** Large attachments are uploaded straight to object storage through presigned URLs; the service only issues them, tracks quota, and reaps unconfirmed uploads.
- **Background work runs in-process.** One replica wins a Postgres advisory lock and becomes the leader, sweeping expired device presence every minute and orphaned blobs hourly. No Celery, no beat scheduler.

---

## Getting started

**Prerequisites:** Python 3.14+, [`uv`](https://docs.astral.sh/uv/), and Docker (for the local Postgres/Redis/MinIO stack).

```bash
cp .env.example .env
```

Fill in at minimum `SUPABASE_URL` — the rest have working local defaults. Then bring up the full stack:

```bash
docker-compose up
```

That starts the API on `:8000` plus Postgres, Redis, and MinIO (console at `:9001`, `minioadmin`/`minioadmin`), and creates the blob bucket. Alternatively run the API on the host against those services:

```bash
uv run uvicorn src.main:app --reload
```

Apply migrations before first use:

```bash
uv run alembic upgrade head
```

Then check it's alive:

```bash
curl http://localhost:8000/internal/healthz
```

Interactive docs are at `/api/docs` (Swagger) and `/api/redoc`, with the schema at
`/api/openapi.json`. All three need `DOCS_ENABLED=true` (already set in
`.env.example`); without it they return 404, so a deployment never publishes them
by accident.

> **Blob testing gotcha:** inside Compose the API signs URLs pointing at the `minio` hostname, which a desktop client on your host cannot resolve. To exercise uploads end-to-end from the real app, run the API on the host with `S3_ENDPOINT_URL=http://localhost:9000`.

---

## Configuration

All settings come from environment variables (or `.env`) via `pydantic-settings`; see `src/config.py` for defaults and [`.env.example`](.env.example) for annotated guidance.

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Postgres connection string with the asyncpg driver. Use Supabase's **connection pooler** host in production. |
| `REDIS_URL` | Pub/sub fan-out and device presence. `redis://:<pw>@redis:6379/0` in the prod compose network; `rediss://` for a TLS endpoint. |
| `SUPABASE_URL` | Required. Identifies the project and derives the JWKS endpoint used to verify tokens. |
| `SUPABASE_JWT_SECRET` | Legacy HS256 secret. Leave blank for projects created from 2025-10-01 onward, which sign asymmetrically. Both schemes are accepted, so a project mid-migration works. |
| `SUPABASE_JWT_AUDIENCE` | Expected `aud` claim, default `authenticated`. |
| `SUPABASE_SERVICE_ROLE_KEY` | Server-only. Enables admin ban/delete through the Supabase Admin API. Never expose to clients. |
| `S3_ENDPOINT_URL`, `S3_BUCKET`, `AWS_*` | Object storage for attachment blobs. |
| `APP_CORS_ORIGINS` | Comma-separated allowed origins, e.g. `tauri://localhost,http://localhost:1420`. |
| `DEFAULT_BLOB_QUOTA_BYTES` | Per-user storage quota, default 50 MB; overridable per user via the admin API. |
| `EMAIL_PROVIDER`, `BREVO_API_KEY` / `SMTP_*`, `EMAIL_FROM` | Outbound mail for sharing invites only — verification and password reset belong to Supabase. |
| `ADMIN_API_KEY` | Gate for `/internal/metrics` and `/internal/v1/*`. Leave empty to disable those endpoints (they return 503). |

---

## API surface

Product endpoints are versioned under `/api/v1`; infrastructure probes deliberately are not, because load balancers and metrics scrapers hardcode their paths. Prefixes come from `src/version.py` — never hardcode `/api/v1`. Every response carries an `X-API-Version` header.

The domains, at a glance:

- **auth** — account bootstrap, master-key and recovery-key storage, device registration, public-key registration and per-device key wrapping.
- **sync** — push, pull, cursor, and a per-type breakdown; last-write-wins.
- **settings** — one encrypted per-user settings blob.
- **blobs** — presigned upload/download, confirm/release, and quota.
- **spaces** — shared spaces, membership, and space-key distribution.
- **invites** — space invitations: list, accept, decline, revoke, and key delivery.
- **realtime** — the `/ws` WebSocket, authenticated with `?token=<jwt>&device_id=<id>`; events are published to `user:{id}` and `space:{id}` channels (`sync:entry`, `space:membership_changed`, `space:rekey`, `invite:*`, and presence) and carry an origin device so a client never re-applies its own write.
- **ops / admin** — unversioned `/internal` probes and metrics, and the versioned `/internal/v1` management API behind `X-Admin-Key`.

The full, current endpoint list, payload shapes, and event protocol live in
[`docs/architecture.md`](docs/architecture.md) — the source of truth for the wire
contract. This overview intentionally does not restate them, so it cannot drift.

---

## The client contract

These bind this service to the desktop app. Changing one side means changing the other.

- **Every device-scoped route needs both** `Authorization: Bearer <jwt>` and an `X-Device-Id` header; the WebSocket takes the same pair as query parameters. Token parsing lives once in `src/dependencies.py` — routes depend on it rather than re-parsing.
- **`entry_type` is singular** — `"clipboard"` or `"note"`.
- **Deletes are tombstones.** Push with `deleted_at` set; tombstones beat any concurrent edit regardless of timestamp.
- **The server cannot read content.** Entry bodies are AES-256-GCM ciphertext bound to their `client_id` as AAD. It stores the user's password-wrapped master key, per-device wrapped copies, and X25519-wrapped group keys — all opaque blobs it has no key for.
- **Space keys are distributed, not derived.** A random per-space key is wrapped separately for each member's public key; membership changes trigger a `space:rekey` event rather than any server-side key handling.

The definitive reference for payload shapes, data models, and the event protocol is [`docs/architecture.md`](docs/architecture.md).

---

## Project structure

```text
src/
├─ main.py              # app composition: middleware, routers, lifespan (Redis + maintenance)
├─ version.py           # single source for API/service versions and route prefixes
├─ config.py            # pydantic-settings configuration
├─ dependencies.py      # JWT verification + X-Device-Id extraction (shared by every route)
├─ middleware.py        # security headers, X-API-Version
├─ database.py          # async engine + session dependency
├─ redis_client.py      # connection pool lifecycle
├─ limiter.py           # slowapi rate limiter instance
├─ background.py        # leader-elected maintenance: presence sweep, orphan blob cleanup
├─ realtime.py          # /ws endpoint, in-process hub, Redis pub/sub fan-out, presence
├─ email.py             # Brevo / SMTP delivery for invites
├─ supabase_admin.py    # Supabase Admin API calls (ban, delete)
├─ auth/                # profiles, bootstrap, devices, public keys; tokens.py verifies JWTs
├─ sync/                # push/pull/cursor/breakdown + last-write-wins service
├─ settings/            # encrypted per-user settings blob
├─ spaces/              # spaces, invites, join approval, space-key distribution
├─ blobs/               # presigned upload/download (s3.py), quota accounting
├─ announcements/       # server-authored messages to users
├─ admin/               # probes and the versioned management API
└─ web/                 # human-facing HTML pages (templates/)

migrations/versions/    # Alembic 0001…0019
tests/                  # pytest: auth, sync, blobs, space invites, announcements
docs/                   # architecture.md, DEPLOY.md, ANNOUNCEMENTS.md
```

Routers stay thin — HTTP concerns in the route, logic in the domain service beside it.

---

## Development

| Task | Command |
| --- | --- |
| Lint | `uv run ruff check src` |
| Types | `uv run ty check src` |
| Tests | `uv run pytest` |
| Run locally | `uv run uvicorn src.main:app --reload` |
| New migration | `uv run alembic revision -m "description"` |
| Apply migrations | `uv run alembic upgrade head` |

`ruff` and `ty` are not on the venv `PATH` — always go through `uv run`. Both must come back clean on any Python change; tests cover logic changes. Suppressing an error with `# type: ignore` is only acceptable for a documented third-party-stub false positive.

Tests replace Redis with `fakeredis` and mint their own HS256 tokens, but they do need a reachable Postgres — `TEST_DATABASE_URL`, defaulting to a `clipboard_test` database on localhost. Create it once against the Compose Postgres:

```bash
docker-compose exec db createdb -U postgres clipboard_test
```

> **Migrations are written, never auto-applied.** Author the revision, but applying it to any real database is a separate, explicitly approved step.

---

## Deployment

The backend is self-hosted on a small VPS in Docker: Caddy (TLS + reverse proxy) in front of the stateless FastAPI service, with Redis co-located for realtime fan-out and presence. Supabase (Postgres + Auth) and Cloudflare R2 (blobs) stay external. Deploys are pull-based with no CI service or registry: a systemd timer on the box polls `main`, and on a new commit it builds the image locally and recreates the container. There is one replica, so a deploy has a ~1-3s window where a request can get a 502 and realtime clients reconnect once — accepted deliberately rather than running an overlap tool ([`docs/DEPLOY.md`](docs/DEPLOY.md)).

The `Dockerfile` builds from the lockfile and honours `$PORT`, so the image also runs unchanged under plain `docker run`. The full walkthrough — provisioning Supabase and R2, applying migrations, the compose/Caddy/deploy setup, verification, and pointing the desktop app at it — is in [`docs/DEPLOY.md`](docs/DEPLOY.md), with a friendlier version on the [docs site](https://orange-copy-paste-app.pages.dev).

---

## Further reading

- [`docs/architecture.md`](docs/architecture.md) — system overview, service boundaries, data models, full API and WebSocket event reference. The source of truth for the wire contract.
- [`docs/DEPLOY.md`](docs/DEPLOY.md) — self-hosting: deployment and ongoing operations, with placeholders for your own host, domain, and secrets.
- [`docs/ANNOUNCEMENTS.md`](docs/ANNOUNCEMENTS.md) — sending a message to users from the server: the calls, the fields, and how to word one.
- Client repo `docs/architecture.md` — how the desktop app consumes this API.
