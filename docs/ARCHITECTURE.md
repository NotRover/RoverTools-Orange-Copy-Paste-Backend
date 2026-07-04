# Orange Clipboard — Backend Architecture

> **Project:** RoverTools Smart Clipboard Backend
> **Status:** Implemented — reflects current source
> **Last updated:** 2026-07-04
> **Stack:** FastAPI + Supabase (Postgres + Auth) + Redis + S3-compatible blob storage

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Service Boundaries](#2-service-boundaries)
3. [Tech Stack](#3-tech-stack)
4. [Data Models](#4-data-models)
5. [API Design](#5-api-design)
6. [Sync Strategy](#6-sync-strategy)
7. [E2E Encryption Design](#7-e2e-encryption-design)
8. [Realtime Architecture](#8-realtime-architecture)
9. [Auth Flow](#9-auth-flow)
10. [Background Maintenance](#10-background-maintenance)
11. [Deployment Model & Cost](#11-deployment-model--cost)
12. [Desktop App Integration](#12-desktop-app-integration)
13. [Directory Layout](#13-directory-layout)
14. [Client / Backend Contract Drift](#14-client--backend-contract-drift)
15. [Live Share Design](#15-live-share-design)
16. [Security Checklist](#16-security-checklist)

---

## 1. System Overview

The backend is a **stateless FastAPI application** that leans on **Supabase for
Postgres + Auth** and keeps everything else vendor-neutral. Because the API holds
no per-connection state that other instances need (realtime fan-out and presence
are coordinated through Redis), it **scales horizontally** — run N replicas behind
a load balancer, no sticky sessions.

Two moving parts you operate (FastAPI + Redis); the rest is managed:

```
┌──────────────────────────────────────────────────────────────────┐
│                       Tauri Desktop App                            │
│   ├─ Supabase Auth (GoTrue)  ── register / login / refresh / reset │
│   ├─ HTTP  ── /api/v1/*   (Authorization: Bearer <supabase JWT>,    │
│   │                        X-Device-Id: <device>)                  │
│   └─ WS    ── /ws?token=<supabase JWT>&device_id=<device>          │
└───────────────┬──────────────────────────────────┬────────────────┘
                │                                   │
        ┌───────▼────────────────┐         ┌────────▼───────────────────────┐
        │  FastAPI (stateless ×N) │──SQL──► │  SUPABASE (managed)             │
        │  auth · sync · settings │  verify │   • Postgres (app schema)       │
        │  blobs · groups+sharing │  JWT──► │   • Auth (users, sessions,      │
        │  /ws realtime · admin   │         │     email verify, pwd reset)    │
        └───┬───────────────┬─────┘         └─────────────────────────────────┘
            │               │
     ┌──────▼─────┐  ┌──────▼────────────────┐
     │  Redis     │  │  S3-compatible (R2)    │
     │  pub/sub   │  │  image / file blobs    │
     │  + presence│  │  (MinIO in dev)        │
     └────────────┘  └────────────────────────┘
```

- **Supabase** owns the database and the identity layer. It is the only intentional
  vendor lock-in.
- **FastAPI** owns the app-specific logic: the sync engine, groups/sharing +
  E2E key distribution, blob upload brokering, presence, and its own WebSocket.
- **Redis** does exactly two things: realtime pub/sub fan-out and device presence.
- **R2** (or any S3-compatible store) holds encrypted binary blobs. Vendor-neutral.
- **No Celery / no worker service.** Background jobs run in-process under a Postgres
  advisory lock (see §10). Email is sent via FastAPI `BackgroundTasks`.

The desktop app remains fully functional offline; sync is opportunistic and resumes
on reconnect.

---

## 2. Service Boundaries

Each package under `src/` owns a router + service (+ models/schemas). They call each
other in-process.

### 2.1 Auth-adjacent (`src/auth/`)

Registration, email verification, login, token refresh, and password reset are
handled by **Supabase Auth** — the client talks to Supabase directly. This module
owns only what the app itself must store:

- **Profile bootstrap** — get-or-create the app profile for a Supabase user and
  return the KDF salt used to derive the UMK.
- **Device registration** — each install registers a device row (carries the device
  public key and, later, the wrapped UMK); returns a `device_id` the client sends
  back as `X-Device-Id`.
- **Public-key management** — store the user's X25519 identity key and per-device
  wrapped UMK for multi-device E2E.

The backend never issues tokens; it only **verifies** the Supabase JWT (HS256,
project secret) on protected routes.

### 2.2 Sync (`src/sync/`)

Owns `sync_entries` (clipboard + notes) and per-device `sync_cursors`.

- Delta push (batches of new/mutated entries) with last-write-wins + tombstones.
- Delta pull since a cursor.
- Publishes `sync:entry` / `sync:delete` to Redis after a write.

### 2.3 Settings (`src/settings/`)

One encrypted blob per user (`user_settings`), last-write-wins by `updated_at`. On a
client-wins write, publishes `settings:updated` so other devices pull.

### 2.4 Blobs (`src/blobs/`)

Brokers direct-to-object-store uploads.

- `request-upload` → presigned PUT URL + `blob_key` (server never buffers bytes).
- `confirm-upload` → marks the blob confirmed.
- `{blob_key}/download-url` → presigned GET URL.
- `quota` → usage (computed on demand: `SUM(size_bytes)` over confirmed blobs) and
  the per-user quota.
- **5 MB per-entry hard cap**; per-user quota default **50 MB** (configurable
  globally and per-user via the admin API).

### 2.5 Groups + Sharing (`src/groups/`)

Both pools and Live Share sessions are `groups` rows (`group_type` = `pool` |
`live_share`) over the same two tables; the sharing endpoints live in
`src/groups/sharing.py`.

- **Pools** — collaborative shared clipboard namespaces (`group_type='pool'`,
  unlimited members). Create, list, get, rotate invite, join, remove member, delete,
  and E2E group-key distribution.
- **Live Share** (`group_type='live_share'`, max 5) — see §15. Invite by email,
  list sessions, change scope, end / leave.

### 2.6 Realtime (`src/realtime.py`)

Single module: the WebSocket endpoint, an in-process connection hub, the Redis
pub/sub bridge, the publish helpers, and device presence. See §8.

### 2.7 Admin (`src/admin/`)

Ops-only, gated by `X-Admin-Key` (disabled → 503 if `ADMIN_API_KEY` unset), except
the public `/internal/healthz`.

- Health, Prometheus metrics, JSON stats (counts from our tables; online-device
  count from Redis presence; storage computed on demand).
- User management over `profiles` (list/detail/quota). **Account state**
  (email, verification, suspension/ban, deletion) is owned by Supabase and delegated
  to the Supabase Admin API (`src/supabase_admin.py`); those calls require
  `SUPABASE_SERVICE_ROLE_KEY`.

---

## 3. Tech Stack

| Concern            | Choice                                | Reason                                                           |
| ------------------ | ------------------------------------- | ---------------------------------------------------------------- |
| API framework      | FastAPI + uvicorn                     | Async, Pydantic v2, native WebSocket, auto OpenAPI               |
| Database           | Supabase **Postgres 16**              | Managed; asyncpg + SQLAlchemy 2 async core; RLS available        |
| Identity / Auth    | Supabase **Auth (GoTrue)**            | Managed signup, email verify, password reset, sessions, JWTs     |
| JWT verification   | PyJWT, **HS256** (project secret)     | We verify only; Supabase signs. Switchable to JWKS later         |
| Cache / realtime   | Redis 7                               | Pub/sub fan-out + device presence (nothing else)                 |
| Blob storage       | Cloudflare **R2** (S3-compatible)     | Direct presigned PUT/GET; zero egress; MinIO for local dev       |
| Background jobs    | in-process asyncio + PG advisory lock | Presence sweep + orphan blob cleanup; no Celery/broker           |
| Email              | Brevo REST (default) or stdlib SMTP   | Sharing invites only; via `BackgroundTasks`                      |
| KDF (E2E)          | Argon2id (client-side)                | Memory-hard; password → UMK                                      |
| Content encryption | AES-256-GCM (client-side)             | AEAD; server stores ciphertext only                              |
| Key exchange       | X25519 (client-side)                  | Multi-device UMK wrapping + group keys                           |

**Core Python dependencies** (`pyproject.toml`):

```
fastapi, uvicorn[standard], pydantic, pydantic-settings,
sqlalchemy[asyncio], asyncpg, alembic,
redis[hiredis], boto3, pyjwt,
python-multipart, httpx, slowapi, email-validator
```

There is **no** `celery`, `python-jose`, `passlib`, or `cryptography` dependency —
tokens are verified (not signed) with PyJWT, and all cryptography is client-side.

---

## 4. Data Models

All IDs are UUID v4. Timestamps are `BIGINT` milliseconds since the Unix epoch (to
match the Tauri app's `u64`). `client_id` preserves the app's local sequential IDs
for dedup.

### 4.1 `profiles`

The app-side record for a Supabase Auth user. `id` **equals** the Supabase
`auth.users.id` (the JWT `sub`); linkage is application-level (no cross-schema FK).
Email, password, and verification state live in Supabase — not here.

```sql
CREATE TABLE profiles (
    id               UUID PRIMARY KEY,            -- = auth.users.id
    display_name     TEXT NOT NULL DEFAULT '',
    kdf_salt         TEXT NOT NULL,               -- base64; Argon2id salt for UMK derivation
    identity_pubkey  TEXT,                        -- base64 X25519 public key (E2E)
    blob_bytes_quota BIGINT NOT NULL DEFAULT 52428800,  -- 50 MB
    created_at       BIGINT NOT NULL,
    updated_at       BIGINT NOT NULL
);
```

Storage used is **not** stored — it is computed on demand from confirmed blobs.

### 4.2 `devices`

```sql
CREATE TABLE devices (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL,          -- profiles.id
    device_name   TEXT NOT NULL DEFAULT '',
    platform      TEXT NOT NULL,          -- 'windows' | 'linux' | 'macos'
    app_version   TEXT NOT NULL DEFAULT '',
    device_pubkey TEXT,                    -- base64 X25519 public key
    wrapped_umk   TEXT,                    -- AES-GCM(shared_secret, UMK); set by a peer device
    revoked       BOOLEAN NOT NULL DEFAULT false,
    created_at    BIGINT NOT NULL,
    last_seen_at  BIGINT NOT NULL
);
CREATE INDEX idx_devices_user_id ON devices(user_id);
```

Session/refresh lifecycle is owned by Supabase. `revoked` is a soft flag for the
device-management UX (revoking also clears `wrapped_umk`).

### 4.3 `sync_entries`

```sql
CREATE TABLE sync_entries (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id          TEXT NOT NULL,
    user_id            UUID NOT NULL,
    device_id          UUID NOT NULL,
    entry_type         TEXT NOT NULL,      -- 'clipboard' | 'note'
    kind               TEXT,               -- 'text'|'image'|'html'|'file'
    encrypted_content  TEXT NOT NULL,      -- base64 AES-256-GCM ciphertext
    encrypted_metadata TEXT,               -- base64 AES-256-GCM: groups, label, pinned, note_title
    created_at         BIGINT NOT NULL,    -- client-originated
    updated_at         BIGINT NOT NULL,    -- client-originated (notes order by this)
    server_ts          BIGINT NOT NULL,    -- server-assigned; the sync cursor
    deleted_at         BIGINT,             -- tombstone (NULL = alive)
    pinned             BOOLEAN NOT NULL DEFAULT false,
    group_ids          UUID[] NOT NULL DEFAULT '{}',   -- server-visible for routing
    blob_key           TEXT,               -- object key; NULL for text
    blob_size          BIGINT,
    CONSTRAINT uniq_client_entry UNIQUE (user_id, client_id, entry_type)
);
CREATE INDEX idx_sync_entries_user_ts ON sync_entries(user_id, server_ts);
CREATE INDEX idx_sync_entries_device  ON sync_entries(device_id);
CREATE INDEX idx_sync_entries_groups  ON sync_entries USING GIN(group_ids);
```

### 4.4 `sync_cursors`

```sql
CREATE TABLE sync_cursors (
    device_id      UUID PRIMARY KEY,
    user_id        UUID NOT NULL,
    last_server_ts BIGINT NOT NULL DEFAULT 0
);
```

### 4.5 `user_settings`

```sql
CREATE TABLE user_settings (
    user_id        UUID PRIMARY KEY,
    encrypted_blob TEXT NOT NULL,   -- AES-256-GCM(UMK, JSON of synced preferences)
    updated_at     BIGINT NOT NULL  -- client-originated; used for LWW
);
```

### 4.6 `groups`

```sql
CREATE TABLE groups (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id          UUID NOT NULL,
    name              TEXT NOT NULL,
    group_type        TEXT NOT NULL DEFAULT 'pool',   -- 'pool' | 'live_share'
    invite_code       TEXT UNIQUE,
    invite_expires_at BIGINT,
    max_members       INT,            -- NULL = unlimited; 5 for live_share
    created_at        BIGINT NOT NULL
);
```

### 4.7 `group_memberships`

```sql
CREATE TABLE group_memberships (
    group_id          UUID NOT NULL,
    user_id           UUID NOT NULL,
    role              TEXT NOT NULL DEFAULT 'member',    -- 'owner'|'admin'|'member'
    wrapped_group_key TEXT,     -- per-member AES-wrapped group key (see §7.4)
    share_scope       TEXT NOT NULL DEFAULT 'clipboard', -- 'clipboard'|'notes'|'both'
    joined_at         BIGINT NOT NULL,
    PRIMARY KEY (group_id, user_id)
);
CREATE INDEX idx_gm_user ON group_memberships(user_id);
```

### 4.8 `blobs`

```sql
CREATE TABLE blobs (
    key         TEXT PRIMARY KEY,
    user_id     UUID NOT NULL,
    mime_type   TEXT NOT NULL,
    size_bytes  BIGINT NOT NULL,
    checksum    TEXT NOT NULL,       -- SHA-256 hex
    confirmed   BOOLEAN NOT NULL DEFAULT false,
    created_at  BIGINT NOT NULL
);
```

> **Migration note:** `migrations/0006_supabase_migration.py` renames `users` →
> `profiles`, drops the Supabase-owned columns (`email`, `password_hash`,
> `email_verified`, `suspended_at`) plus the denormalized `blob_bytes_used`, drops
> `devices.refresh_token_hash` and `blobs.entry_id`, makes `groups.max_members`
> nullable (NULL = unlimited), and lowers the default quota to 50 MB. Migrations
> 0001–0005 remain the historical chain.

---

## 5. API Design

### Conventions

- Base URL: `/api/v1`
- Auth: `Authorization: Bearer <supabase access token>` on all protected routes.
- Device scope: `X-Device-Id: <device_id>` header on device-scoped routes (sync,
  settings, key registration). Bootstrap and device registration do **not** require it.
- Errors: `{"detail": "..."}` + HTTP status.
- Pagination: cursor-based — `?after_ts=<server_ts>&limit=200`.

### 5.0 Versioning

Source of truth: `src/version.py` (`API_VERSION`, `SERVICE_VERSION`).

- **Product API is versioned** in the path: client-facing under `/api/v1`, admin
  under `/internal/v1`. `API_VERSION` bumps (`v2`, …) only on a
  backwards-incompatible contract change; `v1` and `v2` run side by side during a
  migration window.
- **Infra probes are intentionally unversioned**: `/internal/healthz` and
  `/internal/metrics`. Load balancers and Prometheus hardcode these paths and must
  not track a version on each bump.
- **`SERVICE_VERSION`** (semver, e.g. `2.0.0`) is the deployable build version and
  the OpenAPI `version`; it changes freely per release without implying a contract break.
- Every HTTP response carries an **`X-API-Version`** header (= `API_VERSION`).
- **Live schema:** Swagger UI at `/api/docs`, ReDoc at `/api/redoc`, raw spec at
  `/api/openapi.json`. Every route declares a Pydantic `response_model` and a
  docstring (surfaced as OpenAPI summary/description); tags group the surface
  (auth, sync, settings, blobs, groups, sharing, ops, admin). The WebSocket `/ws`
  contract is documented in §5.8 (FastAPI does not emit WebSockets into OpenAPI).

### 5.1 Auth Routes

```
POST   /api/v1/auth/bootstrap
       Body: { display_name? }
       Returns: { user_id, kdf_salt, display_name }
       Idempotent: creates the profile on first call, generates the stable KDF salt,
       and returns it so the client can derive its UMK. Call right after Supabase login.

POST   /api/v1/auth/devices
       Body: { device_name?, platform?, app_version?, device_pubkey? }
       Returns: 201 { device_id }     -- store and send back as X-Device-Id

GET    /api/v1/auth/devices
       Returns: [{ id, device_name, platform, app_version, last_seen_at }]

DELETE /api/v1/auth/devices/{device_id}         -- soft-revoke; clears wrapped_umk

POST   /api/v1/auth/keys/register               -- requires X-Device-Id
       Body: { identity_pubkey, device_pubkey }  (base64 X25519)

POST   /api/v1/auth/devices/{device_id}/key-wrap
       Body: { wrapped_umk }    -- an existing device wraps the UMK for another device
```

> Registration, email verification, login, refresh, and password reset are **not**
> here — the client performs them against Supabase Auth directly.

### 5.2 Sync Routes  (require `X-Device-Id`)

```
POST /api/v1/sync/push
     Body: { entries: [{ client_id, entry_type, kind?, encrypted_content,
             encrypted_metadata?, created_at, updated_at, pinned, deleted_at?,
             blob_key?, blob_size?, group_ids? }] }
     Returns: { accepted: [{ client_id, server_id, server_ts }],
                conflicts: [{ client_id, reason }] }

GET  /api/v1/sync/pull?after_ts=<ts>&limit=200&entry_type=all|clipboard|note
     Returns: { entries: [...], next_cursor: <ts | null> }

POST /api/v1/sync/cursor        Body: { last_server_ts }
GET  /api/v1/sync/status        Returns: { device_id, last_server_ts }
```

### 5.3 Settings Routes

```
GET  /api/v1/settings           Returns: { encrypted_blob, updated_at }  (404 if none)
PUT  /api/v1/settings           Body: { encrypted_blob, updated_at }
     Returns: { updated_at, winner: 'client'|'server', encrypted_blob }
```

### 5.4 Blob Routes

```
POST /api/v1/blobs/request-upload
     Body: { mime_type, size_bytes, checksum }
     Returns: { blob_key, presigned_put_url, expires_in_seconds }   (413 if > 5 MB;
               402 if over quota)

POST /api/v1/blobs/confirm-upload         Body: { blob_key }
GET  /api/v1/blobs/{blob_key}/download-url  Returns: { presigned_get_url, expires_in_seconds }
GET  /api/v1/blobs/quota                    Returns: { used_bytes, quota_bytes }
```

### 5.5 Groups Routes

```
POST   /api/v1/groups                    Body: { name, group_type?: 'pool' } → { group_id, invite_code }
GET    /api/v1/groups
GET    /api/v1/groups/{group_id}
POST   /api/v1/groups/{group_id}/invite  (no body) → { invite_code, expires_at }   -- rotates the code
POST   /api/v1/groups/join               Body: { invite_code, wrapped_group_key? } → { group_id, name, group_type }
DELETE /api/v1/groups/{group_id}/members/{member_user_id}
DELETE /api/v1/groups/{group_id}
POST   /api/v1/groups/{group_id}/keys    Body: { wrapped_keys: [{ user_id, wrapped_group_key }] }
```

### 5.6 Sharing Routes (Live Share)

```
POST   /api/v1/sharing/invite            Body: { email, share_scope: 'clipboard'|'notes'|'both' }
       Returns: 201 { share_group_id, invite_code, expires_at }
       Creates a live_share group (max 5) and emails the invite to `email`.
GET    /api/v1/sharing/sessions          Returns: [{ share_group_id, members[], my_scope, active_since }]
PATCH  /api/v1/sharing/sessions/{id}/scope   Body: { share_scope }
DELETE /api/v1/sharing/sessions/{id}         -- owner: dissolve
DELETE /api/v1/sharing/sessions/{id}/leave   -- member: leave
```

Joining a Live Share uses `POST /api/v1/groups/join` with the invite code.

### 5.7 Internal Routes

Split into **unversioned infra probes** and the **versioned admin API** (see §5.0).

```
-- Unversioned probes (paths are stable across API versions)
GET  /internal/healthz     Returns: { status, db, redis }   -- public, response_model=HealthResponse
GET  /internal/metrics     Prometheus text (X-Admin-Key)     -- excluded from OpenAPI (text/plain)

-- Versioned admin API (require X-Admin-Key)
GET  /internal/v1/stats       JSON aggregate
GET  /internal/v1/admin/users?offset=&limit=&search=<display_name>
GET  /internal/v1/admin/users/{user_id}      -- enriched with email/verified/banned when Supabase admin is configured
PATCH /internal/v1/admin/users/{user_id}/quota   Body: { blob_bytes_quota }
POST  /internal/v1/admin/users/{user_id}/suspend Body: { suspend: bool }   -- delegates to Supabase (ban/unban)
DELETE /internal/v1/admin/users/{user_id}        -- deletes profile (cascade) + Supabase user
```

Metrics: `orange_users_total`, `orange_devices_total`, `orange_devices_active_total`,
`orange_devices_online`, `orange_sync_entries_total`, `orange_sync_entries_deleted_total`,
`orange_blobs_total`, `orange_blobs_confirmed_total`, `orange_storage_bytes_used`,
`orange_redis_memory_bytes`.

### 5.8 WebSocket Event Protocol

**Connection:** `GET /ws?token=<supabase access token>&device_id=<device_id>`

The connection is subscribed to `user:<user_id>` and every `group:<group_id>` the
user belongs to.

**Server → Client events:**

```jsonc
{ "event": "sync:entry",   "payload": { ...sync_entry } }
{ "event": "sync:delete",  "payload": { "server_id": "...", "deleted_at": 1234567 } }
{ "event": "device:online",  "payload": { "device_id": "..." } }
{ "event": "device:offline", "payload": { "device_id": "..." } }
{ "event": "group:membership_changed", "payload": { "group_id": "...", "action": "joined|left", "user_id": "..." } }
{ "event": "group:rekey",  "payload": { "group_id": "...", "wrapped_group_key": "..." } }
{ "event": "sharing:accepted",     "payload": { "share_group_id": "...", "new_member": {...}, "wrapped_group_key": "..." } }
{ "event": "sharing:ended",        "payload": { "share_group_id": "...", "ended_by": "..." } }
{ "event": "sharing:scope_changed","payload": { "share_group_id": "...", "user_id": "...", "share_scope": "..." } }
{ "event": "settings:updated",     "payload": { "updated_at": 1234567 } }
{ "event": "ping",         "payload": { "server_ts": 1234567 } }
```

**Client → Server:** `{ "event": "pong" }` / `{ "event": "ack" }` (either refreshes the presence TTL).

> `sharing:invite` exists as a publish helper but is **not currently emitted** — Live
> Share invites are delivered by email, because mapping an invite email to a user id
> is owned by Supabase (the backend stores no email). Wire it up later via a Supabase
> Admin lookup if live "you've been invited" prompts are wanted.

---

## 6. Sync Strategy

### 6.1 Server-Timestamped Last-Write-Wins

- Server assigns a millisecond `server_ts` on every accepted write; it is the sync
  cursor.
- Conflict on `(user_id, client_id, entry_type)`: last writer wins. A push with an
  `updated_at` not newer than the stored row is rejected as `stale_update`.
- Tombstone wins: an incoming delete beats a concurrent live update.

### 6.2 Push / Pull

```
Push:  client → POST /sync/push [batch] → upsert (client_id dedup), assign server_ts,
       publish sync:entry to Redis → { accepted, conflicts }
Pull:  client → GET /sync/pull?after_ts=<cursor>&limit=200
       server → rows WHERE server_ts > cursor ORDER BY server_ts ASC (+1 for next_cursor)
       repeat until next_cursor = null → POST /sync/cursor
```

### 6.3 Offline Operation

Local `history.bin` / `notes.bin` are the source of truth. The sync client pulls on
startup/reconnect, queues pushes while offline, and applies WS events as they arrive.

---

## 7. E2E Encryption Design

**Unchanged by the Supabase migration.** The invariant holds identically: the server
(and Supabase) store and forward **ciphertext only** — never plaintext content, note
titles, group names, or labels.

### 7.1 User Master Key (UMK)

```
UMK = Argon2id(password, kdf_salt, m=65536, t=3, p=4) → 32 bytes
```

The password is entered once and does double duty: it authenticates to **Supabase
Auth**, and it derives the UMK **locally**. `kdf_salt` is generated by the backend on
first `POST /auth/bootstrap` and returned there (it used to ride the login response).
The UMK lives in memory only.

### 7.2 Content Encryption

```
nonce(12B) || AES-256-GCM(key=UMK, plaintext=content, aad=client_id) → base64 → encrypted_content
```

`encrypted_metadata` encodes `{ groups, label, pinned, note_title }` the same way.

### 7.3 Multi-Device UMK Sharing (X25519)

1. New device generates an X25519 keypair; registers it (`device_pubkey`).
2. An existing device computes `shared_secret = X25519(my_priv, new_device_pubkey)`,
   wraps the UMK, and posts it to `POST /auth/devices/{new_device_id}/key-wrap`.
3. The new device derives the same secret and unwraps the UMK. The server stores
   `wrapped_umk` but can never unwrap it (holds no private key).

### 7.4 Group Key Distribution

Group creator generates a random 32-byte Group Key, wraps it per member with
`X25519(my_priv, member_identity_pubkey)`, and posts all copies to
`POST /groups/{id}/keys`. Group entries are encrypted with the Group Key. On member
removal the owner rotates the key and re-distributes (`group:rekey` WS event).

### 7.5 Server Visibility

Server sees: entry type/kind, timestamps, blob keys, group membership (user ↔ group),
group names. Server never sees: `encrypted_content`, `encrypted_metadata`, or
`user_settings.encrypted_blob`.

---

## 8. Realtime Architecture

`src/realtime.py` is one module: WebSocket endpoint + in-process hub + Redis bridge +
presence + publish helpers.

### 8.1 Fan-out (horizontally scalable)

Each API process keeps an in-memory hub of its **local** sockets and runs one Redis
`psubscribe("user:*", "group:*")` listener. A write publishes an event to Redis; every
process forwards it to its own local sockets on that channel (excluding the origin
device). No sticky sessions; add replicas freely.

### 8.2 Presence (connection-driven)

- **Connect** → `SADD user:{uid}:devices {did}`, `SET presence:{uid}:{did} EX 300`,
  publish `device:online` **immediately**.
- **Clean disconnect** → `SREM`, `DEL presence:{uid}:{did}`, publish `device:offline`
  **immediately**.
- **Heartbeat** — server pings every 25 s; any client message/`pong` refreshes the
  presence TTL.
- **Crash backstop** — a socket that dies without a clean close leaves its presence
  key to expire; the maintenance sweeper (§10) then emits `device:offline`. So the
  instant path is primary and the sweep is a safety net, not a 60 s-latency primary.

---

## 9. Auth Flow

Identity is Supabase's; the app layers device + key state on top.

```
1. Sign up / log in / verify / reset  ──► Supabase Auth (client SDK / GoTrue REST)
        → client holds a Supabase access token (JWT, sub = user id) + refresh token

2. POST /api/v1/auth/bootstrap { display_name? }   (Authorization: Bearer <JWT>)
        → ensures a profile, returns kdf_salt → client derives UMK = Argon2id(password, kdf_salt)

3. POST /api/v1/auth/devices { device_name, platform, ... }
        → { device_id }  (client stores it, sends X-Device-Id on subsequent calls)

4. All app calls: Authorization: Bearer <JWT> [+ X-Device-Id]
        → FastAPI verifies the JWT (HS256, SUPABASE_JWT_SECRET, aud='authenticated')
```

**Token verification** (`src/auth/tokens.py`): PyJWT decode with the project JWT
secret, requiring `exp` and `sub`, audience `authenticated`. No deny-list — Supabase
owns session revocation. To immediately cut off a user, ban them via the admin
suspend endpoint (Supabase).

---

## 10. Background Maintenance

`src/background.py` replaces Celery + beat. A single asyncio loop, started in the app
lifespan, guarded by a **Postgres advisory lock** (`pg_try_advisory_lock`), so across
N replicas exactly one runs it. If the leader dies, its connection drops, the lock
releases, and another replica takes over on its next attempt.

Two jobs:

- **Presence sweep** (every 60 s) — scan `user:*:devices`; for members whose
  `presence:{uid}:{did}` key has expired, `SREM` and publish `device:offline`.
- **Orphan blob cleanup** (hourly) — delete unconfirmed blobs older than 1 h from the
  object store and the `blobs` table.

Email (sharing invites) is sent separately via FastAPI `BackgroundTasks` (best-effort;
failures are logged, not surfaced to the request).

---

## 11. Deployment Model & Cost

### 11.1 Development (`docker-compose.yml`)

```
api    — FastAPI uvicorn --reload (:8000)
db     — postgres:16-alpine  (local stand-in for Supabase Postgres)
redis  — redis:7-alpine
```

Blobs use MinIO or a real R2 bucket via env. No worker service.

### 11.2 Production

- **Supabase** (managed): Postgres + Auth. Point `DATABASE_URL` at the Supabase
  connection string (asyncpg driver) and set `SUPABASE_URL` / `SUPABASE_JWT_SECRET`.
- **FastAPI**: deploy anywhere (Fly/Render/etc.); scale to N stateless replicas behind
  a load balancer. Nginx/ingress must allow the `/ws` upgrade with a long read timeout.
- **Redis**: managed (e.g. Upstash, `rediss://`).
- **R2**: bucket + credentials via the S3 env vars (`AWS_REGION=auto`).

### 11.3 Free-tier ceilings & cost

- **Supabase free**: 500 MB Postgres + 5 GB egress; free projects **pause after ~1
  week idle**. Text entries are tiny, so the DB is rarely the wall. The meaningful
  first bill is **Supabase Pro (~$25/mo)** for always-on + headroom.
- **R2 free**: 10 GB storage, **zero egress**. Binary blobs are the real storage cost;
  even beyond free it is ~$0.015/GB-mo. At the **50 MB** default per-user quota, the
  10 GB free pool covers ~200 users before R2 costs anything.
- **Redis**: presence + pub/sub only — well within any free managed tier.

Per-user storage is capped by `profiles.blob_bytes_quota` (default 50 MB, set via
`DEFAULT_BLOB_QUOTA_BYTES`, overridable per user via the admin quota endpoint) and a
5 MB per-entry hard cap.

---

## 12. Desktop App Integration

The Rust `src-tauri/src/sync/` module (client, `ws_listener`, crypto, offline queue)
is largely unchanged. What the client must adopt for this backend:

1. **Auth via Supabase.** Obtain the access token from Supabase Auth (GoTrue), not
   from a backend `/auth/login`. Send it as `Authorization: Bearer <token>`.
2. **Bootstrap for the KDF salt.** Call `POST /auth/bootstrap` after login to get
   `kdf_salt` (previously returned by `/auth/login`), then derive the UMK.
3. **Register a device, send `X-Device-Id`.** Call `POST /auth/devices` once, persist
   `device_id`, and send it as the `X-Device-Id` header on device-scoped calls.
4. **WebSocket** connects to `/ws?token=<supabase JWT>&device_id=<device_id>`.
5. **Deletion is a tombstone** in `POST /sync/push` (`deleted_at` set) — there is no
   dedicated delete route.

Image/file blobs still upload directly to R2 via presigned PUT (`request-upload` →
PUT → `confirm-upload`), subject to the 5 MB per-entry cap; text/metadata ride inside
the encrypted sync payload. Local store stays plaintext; encryption happens at the
network boundary.

---

## 13. Directory Layout

```
orange-copy-paste-clipboard-backend/
├── src/
│   ├── main.py               # app factory, router mounts, lifespan (listener + maintenance)
│   ├── config.py             # pydantic-settings (Supabase, Redis, S3, email, admin)
│   ├── database.py           # SQLAlchemy async engine + session factory
│   ├── redis_client.py       # Redis connection pool
│   ├── dependencies.py       # get_current_user_only / get_current_user_id / get_redis
│   ├── realtime.py           # WS endpoint + hub + Redis bridge + presence + publishers
│   ├── background.py         # advisory-lock maintenance loop (presence sweep, blob cleanup)
│   ├── email.py              # sharing-invite email (Brevo | SMTP), via BackgroundTasks
│   ├── supabase_admin.py     # Supabase Auth Admin API client (get/ban/delete user)
│   ├── middleware.py         # security headers
│   ├── limiter.py            # slowapi limiter instance
│   ├── auth/                 # tokens.py (JWT verify) + router/service/models/schemas
│   ├── sync/                 # router/service/models/schemas
│   ├── settings/             # router/service/models/schemas
│   ├── blobs/                # router/service/models/schemas + s3.py
│   ├── groups/               # router/service/models/schemas + sharing.py (Live Share)
│   └── admin/                # router/service/schemas
├── migrations/               # Alembic (0001–0006)
├── tests/                    # conftest + test_auth / test_sync / test_blobs
├── docker-compose.yml        # api + db + redis (dev)
├── Dockerfile
├── pyproject.toml
└── .env.example
```

There is no `worker/`, `realtime/` package, `email/` package, `sharing/` package,
`well_known.py`, or `auth/jwt.py` — those were removed or collapsed.

---

## 14. Client / Backend Contract Drift

The current Rust client (a separate submodule) calls a few routes that this backend
does **not** implement in this shape. These are **flagged, not fixed** here — the
client will be completed to match the backend. Reconcile when building the client:

| Client currently calls | Backend reality |
| ----------------------- | ---------------- |
| `POST /api/v1/auth/login`, `/refresh`, `/logout` | Gone — use **Supabase Auth** (GoTrue) directly, then `POST /auth/bootstrap`. |
| `DELETE /api/v1/sync/entries/{server_id}` | No such route — delete via a **tombstone** in `POST /sync/push`. |
| `POST /api/v1/sharing` (create) | Use `POST /api/v1/sharing/invite`. |
| `POST /api/v1/sharing/join` | Join via `POST /api/v1/groups/join` (invite code). |
| `PUT  /api/v1/sharing/sessions/{id}/scope` | Method is **PATCH**. |
| `GET /ws?token=<jwt>` | Now also requires `&device_id=<device_id>`; `token` is the Supabase JWT. |
| (implicit) device id from JWT claim | Now a registered device via `POST /auth/devices`, sent as `X-Device-Id`. |

---

## 15. Live Share Design

Live Share lets up to 5 users share clipboard entries and/or notes in real time,
built entirely on the group + sync + realtime infrastructure.

### 15.1 Establishing a Share

```
Owner → POST /api/v1/sharing/invite { email, share_scope }
        → creates a live_share group (max 5), emails the invite code to `email`
Invitee → POST /api/v1/groups/join { invite_code, wrapped_group_key }
        → added to the group; owner receives `sharing:accepted` (with the wrapped key) via WS
```

### 15.2 Live Entry Fan-out

When a member copies something whose type matches their `share_scope`, the client tags
the entry with the `share_group_id`, encrypts it with the **Group Key** (not the UMK),
and pushes it. The server stores it and publishes `sync:entry` to `group:{id}`; every
member's socket receives and decrypts it locally.

### 15.3 Scope / End / Leave

- `PATCH /sharing/sessions/{id}/scope` → updates the caller's `share_scope`; publishes
  `sharing:scope_changed`.
- `DELETE /sharing/sessions/{id}` (owner) → deletes the group; publishes
  `sharing:ended` first.
- `DELETE /sharing/sessions/{id}/leave` (member) → removes only the caller.

Ending a session abandons the Group Key; historical entries remain readable locally by
those who already had them. Live Share keys are independent of pool group keys.

---

## 16. Security Checklist

- [x] All `/api/v1/*` routes (except `/auth/bootstrap`'s own JWT gate) require a valid
      Supabase JWT; `/internal/*` (except `/healthz`) require `X-Admin-Key`.
- [x] Backend verifies, never signs, tokens (PyJWT HS256 + `SUPABASE_JWT_SECRET`,
      audience `authenticated`, `exp` required).
- [x] Session/refresh/verification/reset owned by Supabase Auth; account
      suspension/deletion delegated to the Supabase Admin API.
- [x] Device private keys never leave the client; the server stores only public keys
      and opaque `wrapped_umk` / `wrapped_group_key`.
- [x] Server stores only ciphertext for `encrypted_content`, `encrypted_metadata`,
      `encrypted_blob`; blobs are client-encrypted before upload.
- [x] Presigned R2 PUT URLs expire in 5 min; GET in 1 h.
- [x] Per-entry 5 MB cap + per-user quota (default 50 MB) enforced server-side.
- [x] `SUPABASE_SERVICE_ROLE_KEY` is server-only and never returned to clients.
- [x] Security headers (`X-Content-Type-Options`, `X-Frame-Options`, CSP,
      `Referrer-Policy`, `Permissions-Policy`, conditional HSTS) via middleware.
- [x] All SQL via SQLAlchemy parameterized queries.
- [ ] Move JWT verification from the shared HS256 secret to Supabase's asymmetric
      JWKS when key rotation is desired (config-only switch).
```
