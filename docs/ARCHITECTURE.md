# Orange Clipboard — Backend Architecture

> **Project:** RoverTools Smart Clipboard Backend
> **Status:** Design document — implementation in progress
> **Last updated:** 2026-04-16

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Service Boundaries](#2-service-boundaries)
3. [Tech Stack Decisions](#3-tech-stack-decisions)
4. [Data Models](#4-data-models)
5. [API Design](#5-api-design)
6. [Sync Strategy](#6-sync-strategy)
7. [E2E Encryption Design](#7-e2e-encryption-design)
8. [Realtime Architecture](#8-realtime-architecture)
9. [Auth Flow](#9-auth-flow)
10. [Deployment Model](#10-deployment-model)
11. [Desktop App Integration](#11-desktop-app-integration)
12. [Directory Layout](#12-directory-layout)
13. [Implementation Sequencing](#13-implementation-sequencing)

---

## 1. System Overview

The backend is a **semi-microservice FastAPI application**: a single Python process with strict internal module separation. Logical "services" are independent Python packages under `src/`, each owning its own router, models, and business logic. They communicate within-process via direct function calls and async queues — no inter-process RPC overhead at the initial scale target.

A separate Celery worker process (same codebase, different entry point) handles background jobs: large binary uploads, push fan-out, scheduled cleanup. Redis serves as both the Celery broker and the realtime pub/sub bus. PostgreSQL is the primary data store.

```
┌────────────────────────────────────────────────────────────────────┐
│                     Tauri Desktop App                              │
│  ┌──────────────────┐          ┌──────────────────────────────┐   │
│  │ SyncClient (Rust)│──HTTP───►│                              │   │
│  │ + WS connection  │◄─WS─────│   FastAPI  (uvicorn)         │   │
│  └──────────────────┘          │                              │   │
└────────────────────────────────│   /auth   /sync   /ws        │───┘
                                 │   /groups /blobs  /internal  │
                                 └───────────┬──────────────────┘
                                             │
                        ┌────────────────────┼─────────────────────┐
                        ▼                    ▼                      ▼
                ┌──────────────┐    ┌────────────────┐   ┌─────────────────┐
                │  PostgreSQL  │    │  Redis 7        │   │  S3-compatible  │
                │  (primary)   │    │  (cache + pub/  │   │  (image/binary  │
                │              │    │   sub + queue)  │   │   blobs)        │
                └──────────────┘    └────────────────┘   └─────────────────┘
                                             │
                                    ┌────────▼────────┐
                                    │  Celery Worker  │
                                    │  (async tasks)  │
                                    └─────────────────┘
```

The desktop app remains **fully functional offline**. The sync client runs as a background task inside the Tauri process. All network communication is opportunistic — the app syncs on reconnect.

---

## 2. Service Boundaries

### 2.1 Auth Service (`src/auth/`)

**Owns:** Users, Devices, Sessions, refresh tokens, password hashing.

**Responsibilities:**
- User registration and login
- Device registration (each install = a device with its own token pair)
- JWT access token issuance (15-minute expiry)
- Refresh token rotation (7-day expiry, stored hashed in PostgreSQL)
- Password reset flow (email-based token link)

### 2.2 Sync Service (`src/sync/`)

**Owns:** SyncEntries (clipboard + notes), SyncCursor per device, conflict resolution.

**Responsibilities:**
- Receiving delta uploads from devices (POST batches of new/mutated entries)
- Serving delta downloads to devices (GET since a cursor position)
- Conflict resolution (last-write-wins with server timestamp as tiebreaker)
- Tombstone management (deleted entries are tombstoned for 30 days)
- Triggering realtime fan-out via Redis pub/sub after a successful write

### 2.3 Blob Service (`src/blobs/`)

**Owns:** Pre-signed upload/download URL generation, blob metadata records.

**Responsibilities:**
- Generating S3 pre-signed PUT URLs for image/binary uploads (client uploads directly to S3)
- Generating S3 pre-signed GET URLs for downloads
- Recording blob metadata (size, mime type, checksum, owning entry id)
- Enforcing per-user storage quota
- Scheduling orphan blob cleanup via Celery task

### 2.4 Groups Service (`src/groups/`)

**Owns:** UserGroups (team groups), GroupMemberships, group-scoped shared clipboard pools.

**Responsibilities:**
- Creating and naming groups
- Inviting members by email (generates a time-limited invite token)
- Accepting/declining invites
- Shared clipboard: a group has a shared sync namespace; group members see each other's pushes in real-time
- E2E group key distribution (see §7.4)

### 2.5 Realtime Hub (`src/realtime/`)

**Owns:** WebSocket connection lifecycle, Redis pub/sub bridge, device presence tracking.

**Responsibilities:**
- Accepting WebSocket upgrades at `/ws`
- Authenticating the WS connection (JWT in query param)
- Subscribing each connection to its user's Redis channel and any group channels
- Fanning out events from Redis to connected clients
- Tracking which devices are currently connected (Redis SET with TTL)

### 2.6 Admin Service (`src/admin/`)

**Owns:** Health endpoint, internal metrics endpoint (Prometheus-format).

---

## 3. Tech Stack Decisions

| Concern | Choice | Reason |
|---|---|---|
| API framework | FastAPI + uvicorn | Async, Pydantic v2, native WebSocket, auto OpenAPI |
| Database | PostgreSQL 16 | Multi-process safe, JSONB, row-level security path, asyncpg |
| ORM/query | SQLAlchemy 2 async core | No ORM overhead on hot paths, typed queries |
| Cache + pub/sub | Redis 7 | Celery broker, fan-out bus, JWT deny-list, presence TTL |
| Blob storage | S3-compatible (MinIO dev / R2 prod) | Client uploads direct via pre-signed PUT; API never buffers blobs |
| Task queue | Celery 5 + Redis | Email delivery, blob cleanup, large batch offload |
| JWT signing | RS256 (asymmetric) | Public key exposable at `/.well-known/jwks.json` |
| Password hashing | bcrypt (passlib) | Industry standard, tunable cost |
| KDF (E2E) | Argon2id | Memory-hard, OWASP recommended for password-derived keys |
| Content encryption | AES-256-GCM | AEAD, hardware-accelerated, widely supported |
| Key exchange | X25519 (ECDH) | Fast, secure, used for multi-device UMK wrapping + group keys |

**Core Python dependencies:**
```
fastapi>=0.115
uvicorn[standard]>=0.30
pydantic>=2.7
pydantic-settings>=2.3
sqlalchemy[asyncio]>=2.0
asyncpg>=0.29
alembic>=1.13
redis[hiredis]>=5.0
celery[redis]>=5.4
boto3>=1.34
python-jose[cryptography]>=3.3
passlib[bcrypt]>=1.7
python-multipart>=0.0.9
cryptography>=42
```

---

## 4. Data Models

All IDs are UUID v4 (server-generated). Timestamps are `BIGINT` milliseconds since Unix epoch to match the Tauri app's `u64` convention. `client_id` preserves the Tauri app's local sequential IDs for dedup.

### 4.1 `users`
```sql
CREATE TABLE users (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email            TEXT UNIQUE NOT NULL,
    display_name     TEXT NOT NULL DEFAULT '',
    password_hash    TEXT NOT NULL,
    kdf_salt         TEXT NOT NULL,          -- base64; Argon2id salt for UMK derivation
    identity_pubkey  TEXT,                   -- base64 X25519 public key (E2E)
    email_verified   BOOLEAN NOT NULL DEFAULT false,
    blob_bytes_used  BIGINT NOT NULL DEFAULT 0,
    blob_bytes_quota BIGINT NOT NULL DEFAULT 524288000,  -- 500 MB
    created_at       BIGINT NOT NULL,
    updated_at       BIGINT NOT NULL
);
```

### 4.2 `devices`
```sql
CREATE TABLE devices (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_name        TEXT NOT NULL DEFAULT '',
    platform           TEXT NOT NULL,       -- 'windows' | 'linux' | 'macos'
    app_version        TEXT NOT NULL DEFAULT '',
    device_pubkey      TEXT,                -- base64 X25519 public key
    wrapped_umk        TEXT,               -- AES-GCM(shared_secret, UMK); set by peer device
    refresh_token_hash TEXT,               -- bcrypt of current refresh token
    revoked            BOOLEAN NOT NULL DEFAULT false,
    created_at         BIGINT NOT NULL,
    last_seen_at       BIGINT NOT NULL
);
CREATE INDEX idx_devices_user_id ON devices(user_id);
```

### 4.3 `sync_entries`
```sql
CREATE TABLE sync_entries (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id           TEXT NOT NULL,       -- Tauri local ID (e.g. "42")
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_id           UUID NOT NULL REFERENCES devices(id),
    entry_type          TEXT NOT NULL,       -- 'clipboard' | 'note'
    kind                TEXT,               -- 'text' | 'image' | 'file' | 'html' (clipboard only)

    -- E2E encrypted payloads (server stores ciphertext only)
    encrypted_content   TEXT NOT NULL,       -- base64 AES-256-GCM ciphertext
    encrypted_metadata  TEXT,               -- base64 AES-256-GCM: groups, label, pinned, note_title

    -- Sync bookkeeping
    created_at          BIGINT NOT NULL,     -- client-originated timestamp
    updated_at          BIGINT NOT NULL,     -- client-originated; notes use this for ordering
    server_ts           BIGINT NOT NULL,     -- server-assigned; used as sync cursor
    deleted_at          BIGINT,             -- tombstone (NULL = alive)
    pinned              BOOLEAN NOT NULL DEFAULT false,

    -- Group membership (server-visible for routing; names are inside encrypted_metadata)
    group_ids           UUID[] NOT NULL DEFAULT '{}',

    -- Blob-backed content (images)
    blob_key            TEXT,               -- S3 object key; NULL for text entries
    blob_size           BIGINT,

    CONSTRAINT uniq_client_entry UNIQUE (user_id, client_id, entry_type)
);
CREATE INDEX idx_sync_entries_user_ts ON sync_entries(user_id, server_ts);
CREATE INDEX idx_sync_entries_device  ON sync_entries(device_id);
CREATE INDEX idx_sync_entries_groups  ON sync_entries USING GIN(group_ids);
```

### 4.4 `sync_cursors`
```sql
CREATE TABLE sync_cursors (
    device_id      UUID PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
    user_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    last_server_ts BIGINT NOT NULL DEFAULT 0
);
```

### 4.5 `groups`
```sql
CREATE TABLE groups (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id         UUID NOT NULL REFERENCES users(id),
    name             TEXT NOT NULL,
    invite_code      TEXT UNIQUE,
    invite_expires_at BIGINT,
    created_at       BIGINT NOT NULL
);
```

### 4.6 `group_memberships`
```sql
CREATE TABLE group_memberships (
    group_id          UUID NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    user_id           UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role              TEXT NOT NULL DEFAULT 'member',  -- 'owner' | 'admin' | 'member'
    wrapped_group_key TEXT,     -- per-member AES-wrapped group key (see §7.4)
    joined_at         BIGINT NOT NULL,
    PRIMARY KEY (group_id, user_id)
);
CREATE INDEX idx_gm_user ON group_memberships(user_id);
```

### 4.7 `blobs`
```sql
CREATE TABLE blobs (
    key         TEXT PRIMARY KEY,
    user_id     UUID NOT NULL REFERENCES users(id),
    entry_id    UUID REFERENCES sync_entries(id),
    mime_type   TEXT NOT NULL,
    size_bytes  BIGINT NOT NULL,
    checksum    TEXT NOT NULL,       -- SHA-256 hex
    confirmed   BOOLEAN NOT NULL DEFAULT false,
    created_at  BIGINT NOT NULL
);
```

---

## 5. API Design

### Conventions
- Base URL: `/api/v1`
- Auth: `Authorization: Bearer <access_token>` on all protected routes
- Errors: `{"detail": "...", "code": "ERROR_CODE"}` + HTTP status
- Pagination: cursor-based — `?after_ts=<server_ts>&limit=200`

### 5.1 Auth Routes
```
POST   /api/v1/auth/register
       Body: { email, password, display_name }
       Returns: { user_id }

POST   /api/v1/auth/login
       Body: { email, password, device_name, platform, app_version, device_pubkey? }
       Returns: { access_token, refresh_token, device_id, kdf_salt, user: {...} }

POST   /api/v1/auth/refresh
       Body: { refresh_token, device_id }
       Returns: { access_token, refresh_token }

POST   /api/v1/auth/logout
       Body: { device_id }

DELETE /api/v1/auth/devices/{device_id}

GET    /api/v1/auth/devices
       Returns: [{ device_id, device_name, platform, last_seen_at, is_current }]

POST   /api/v1/auth/password-reset/request
       Body: { email }

POST   /api/v1/auth/password-reset/confirm
       Body: { token, new_password }

POST   /api/v1/auth/keys/register
       Body: { identity_pubkey, device_pubkey }

POST   /api/v1/auth/devices/{device_id}/key-wrap
       Body: { wrapped_umk }    -- existing device wraps UMK for new device
```

### 5.2 Sync Routes
```
POST   /api/v1/sync/push
       Body: { entries: [{ client_id, entry_type, kind?, note_title?,
               encrypted_content, encrypted_metadata?, created_at, updated_at,
               pinned, deleted_at?, blob_key?, group_ids? }] }
       Returns: { accepted: [{ client_id, server_id, server_ts }],
                  conflicts: [{ client_id, reason }] }

GET    /api/v1/sync/pull
       Query: ?after_ts=<ts>&limit=200&entry_type=all|clipboard|note
       Returns: { entries: [...], next_cursor: <ts | null> }

POST   /api/v1/sync/cursor
       Body: { last_server_ts }

GET    /api/v1/sync/status
       Returns: { device_id, last_server_ts }
```

### 5.3 Blob Routes
```
POST   /api/v1/blobs/request-upload
       Body: { mime_type, size_bytes, checksum, entry_client_id }
       Returns: { blob_key, presigned_put_url, expires_in_seconds }

POST   /api/v1/blobs/confirm-upload
       Body: { blob_key }

GET    /api/v1/blobs/{blob_key}/download-url
       Returns: { presigned_get_url, expires_in_seconds }

GET    /api/v1/blobs/quota
       Returns: { used_bytes, quota_bytes }
```

### 5.4 Groups Routes
```
POST   /api/v1/groups
       Body: { name }
       Returns: { group_id, invite_code }

GET    /api/v1/groups
GET    /api/v1/groups/{group_id}

POST   /api/v1/groups/{group_id}/invite
       Body: { email? }
       Returns: { invite_code, expires_at }

POST   /api/v1/groups/join
       Body: { invite_code, wrapped_group_key }
       Returns: { group_id, name }

DELETE /api/v1/groups/{group_id}/members/{user_id}
DELETE /api/v1/groups/{group_id}

POST   /api/v1/groups/{group_id}/keys
       Body: { wrapped_keys: [{ user_id, wrapped_group_key }] }
```

### 5.5 WebSocket Event Protocol

**Connection:** `GET /ws?token=<access_token>`

Server subscribes the connection to `user:<user_id>` and all `group:<group_id>` channels.

**Server → Client events:**
```jsonc
{ "event": "sync:entry",   "payload": { ...sync_entry } }
{ "event": "sync:delete",  "payload": { "server_id": "...", "deleted_at": 1234567 } }
{ "event": "device:online",  "payload": { "device_id": "...", "device_name": "..." } }
{ "event": "device:offline", "payload": { "device_id": "..." } }
{ "event": "group:membership_changed", "payload": { "group_id": "...", "action": "joined|left", "user_id": "..." } }
{ "event": "group:rekey",  "payload": { "group_id": "...", "wrapped_group_key": "..." } }
{ "event": "ping",         "payload": { "server_ts": 1234567 } }
```

**Client → Server:**
```jsonc
{ "event": "ack",  "payload": { "server_ts": 1234567 } }
{ "event": "pong" }
```

---

## 6. Sync Strategy

### 6.1 Server-Timestamped Last-Write-Wins

Most clipboard entries are append-only (new captures). Mutation conflicts (pin/group changes) are rare and typically single-device. Full vector clocks add client complexity without meaningful benefit here.

- Server assigns monotonic `server_ts` using `clock_timestamp()` in milliseconds
- Conflict: two devices push the same `(user_id, client_id, entry_type)` → last writer wins, new `server_ts` assigned
- Tombstone wins: if one side deletes and another updates, deletion propagates

### 6.2 Delta Push Flow
```
Client → POST /sync/push [batch of entries]
Server → upsert each (client_id dedup), assign server_ts, publish to Redis
Client ← { accepted: [{ client_id, server_id, server_ts }], conflicts: [...] }
Client stores client_id → server_id mapping for cross-device reference
```

### 6.3 Delta Pull Flow
```
On startup / reconnect:
  Client → GET /sync/pull?after_ts={last_server_ts}&limit=200
  Server → entries WHERE server_ts > last_server_ts ORDER BY server_ts ASC
  Repeat until next_cursor = null
  Client → POST /sync/cursor { last_server_ts }
```

### 6.4 Offline Operation

The Tauri app uses its local `history.bin` / `notes.bin` as the source of truth. The sync module:
1. On startup: authenticate + pull delta
2. On new entry: encrypt + push; if offline, queue to `sync_pending.json`
3. On reconnect: flush pending queue, then pull delta
4. On WebSocket event: decrypt + insert into local store, emit Tauri event to update UI

### 6.5 Conflict Resolution Table

| Scenario | Resolution |
|---|---|
| Same `client_id`, newer `updated_at` | Accept, new `server_ts` |
| Same `client_id`, simultaneous push | Last HTTP request wins |
| Deleted locally, updated remotely | Tombstone wins |
| `pinned` diverges across devices | Last write by `server_ts` |

---

## 7. E2E Encryption Design

**Invariant:** The server stores and forwards ciphertext only. It never sees plaintext content, note titles, group tag names, or labels.

### 7.1 User Master Key (UMK)

Derived on the device from the user's password:
```
UMK = Argon2id(password, kdf_salt, m=65536, t=3, p=4) → 32 bytes
```
`kdf_salt` is a random 16-byte value stored in `users.kdf_salt` (returned at login). The UMK lives in memory only — never persisted to disk.

### 7.2 Content Encryption

```
nonce (12 bytes, random) || AES-256-GCM(key=UMK, plaintext=content, aad=client_id)
→ base64-encode → encrypted_content
```

AAD (additional authenticated data) = `client_id` binds ciphertext to the entry, preventing transplanting attacks.

`encrypted_metadata` encodes `{ groups, label, pinned, note_title }` as JSON, encrypted the same way.

### 7.3 Multi-Device UMK Sharing (X25519 Key Handshake)

When a new device registers:
1. New device generates X25519 keypair; sends `device_pubkey` to server at login
2. User approves on an existing device (prompted via WS event or next app open)
3. Existing device:
   - Fetches `device_pubkey` from `GET /api/v1/auth/devices`
   - Computes `shared_secret = X25519(my_privkey, new_device_pubkey)`
   - `wrapped_umk = nonce || AES-256-GCM(key=shared_secret, plaintext=UMK)`
   - Posts to `POST /api/v1/auth/devices/{new_device_id}/key-wrap`
4. New device fetches its `wrapped_umk`, derives `shared_secret` symmetrically, decrypts UMK
5. Server stores `wrapped_umk` per device row but can never unwrap it (holds neither private key)

Device private keys are stored in the OS keychain via `tauri-plugin-stronghold` or `keyring`.

### 7.4 Group Key Distribution

1. Group creator generates random 32-byte Group Key (GK)
2. For each member, fetches their `identity_pubkey`
3. Per member: `wrapped_GK = nonce || AES-256-GCM(key=X25519(my_privkey, member_pubkey), plaintext=GK)`
4. Posts all wrapped copies via `POST /api/v1/groups/{group_id}/keys`
5. Group entries encrypted with GK instead of UMK
6. On member removal: owner generates a new GK and re-distributes via `group:rekey` WS event

### 7.5 Server Visibility Summary

| Field | Server sees |
|---|---|
| Entry type / kind | Yes (routing, quota) |
| Timestamps | Yes |
| Blob key | Yes (pre-signed URL generation) |
| Group membership (user_id ↔ group_id) | Yes |
| Group name | Yes |
| `encrypted_content` | Ciphertext only |
| `encrypted_metadata` (groups, label, note_title, pinned) | Ciphertext only |

---

## 8. Realtime Architecture

### 8.1 WebSocket Hub

`src/realtime/hub.py` maintains a process-global registry:
```python
# channel_name → set of active WebSocket connections
connections: dict[str, set[WebSocket]]
```

Each connection is authenticated on connect (JWT in query param). The hub subscribes to Redis channels for the user and all their groups.

### 8.2 Redis Pub/Sub Fan-Out

After sync service writes an entry:
```python
await redis.publish(f"user:{user_id}", json.dumps(event))
for gid in entry.group_ids:
    await redis.publish(f"group:{gid}", json.dumps(event))
```

Hub's async subscriber receives the message and forwards to all local WebSocket connections on that channel, **except the originating device** (filtered by `device_id` in the event payload).

### 8.3 Device Presence

On WebSocket connect:
```
SADD user:{user_id}:devices {device_id}
EXPIRE user:{user_id}:devices 300
```
Refreshed on each client `pong`. TTL expiry triggers `device:offline` event via a Celery periodic task.

### 8.4 Multi-Process Scaling

At scale, multiple uvicorn workers run independently. Each worker subscribes to the same Redis channels. Redis pub/sub delivers to all subscribers; each worker forwards only to its own in-process connections. No sticky sessions required — the fan-out is channel-level, not connection-level.

---

## 9. Auth Flow

### 9.1 Registration
```
Client → POST /auth/register { email, password, display_name }
Server → hash password (bcrypt), generate kdf_salt, insert user, queue verification email
Client ← 201 { user_id }
```

### 9.2 Login + Device Registration
```
Client → POST /auth/login { email, password, device_name, platform, app_version, device_pubkey }
Server → verify bcrypt, upsert device, issue JWT (RS256, 15 min), issue refresh token
         return kdf_salt so client can derive UMK
Client ← { access_token, refresh_token, device_id, kdf_salt, user: {...} }
Client → derives UMK = Argon2id(password, kdf_salt)
Client → if first device: sets own wrapped_umk = AES-GCM(self_shared_secret, UMK)
```

### 9.3 Token Refresh
```
Client intercepts 401 → POST /auth/refresh { refresh_token, device_id }
Server → verify bcrypt(refresh_token, stored_hash), rotate both tokens
Client ← { access_token, refresh_token }  (new refresh_token replaces old)
```

### 9.4 JWT Claims
```json
{
  "sub": "<user_uuid>",
  "did": "<device_uuid>",
  "jti": "<random_uuid>",
  "iat": 1713225600,
  "exp": 1713226500,
  "iss": "orange-clipboard-api"
}
```
Signed RS256. Revocation: Redis SET `revoked:{jti}` with TTL = remaining token lifetime.

---

## 10. Deployment Model

### 10.1 Development (Docker Compose)
```yaml
services:
  api:     FastAPI uvicorn --reload, port 8000
  worker:  Celery worker -c 4
  db:      postgres:16-alpine
  redis:   redis:7-alpine
  minio:   minio/minio (S3-compatible, ports 9000/9001)
```

### 10.2 Production (~100–1000 users, single VPS)
- 4 vCPU / 8 GB RAM VPS
- Nginx: TLS termination, `/ws` upgrade, static asset serving
- `uvicorn --workers 4` (one per core)
- Celery 4 concurrent workers
- PostgreSQL on-host, daily `pg_dump` to S3
- Redis on-host with `appendonly yes`
- Cloudflare R2 for blobs (zero egress cost)

**nginx `/ws` config:**
```nginx
location /ws {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 86400;
}
```

### 10.3 Scale to 10k+ Users
1. Separate API + worker into independent deployable units (stateless API scales horizontally)
2. Move PostgreSQL to managed service (Supabase / Neon / RDS)
3. Move Redis to managed (Upstash / ElastiCache)
4. S3 already external — no change needed
5. No application code changes required for API tier scale-out

---

## 11. Desktop App Integration

### 11.1 New Tauri Module: `src-tauri/src/sync/`
```
src-tauri/src/sync/
  mod.rs            -- exports SyncClient, init
  client.rs         -- reqwest HTTP client, token management, retry with backoff
  ws_listener.rs    -- WebSocket connection, Redis event dispatch to Tauri event bus
  pending_queue.rs  -- local sync_pending.json for offline accumulation
  crypto.rs         -- Argon2id UMK derivation, AES-256-GCM, X25519 key exchange
  commands.rs       -- Tauri commands: sync_login, sync_logout, sync_now, sync_get_status
  config.rs         -- server URL, sync enabled flag
```

**New Cargo dependencies:**
```toml
reqwest = { version = "0.12", features = ["json", "rustls-tls"] }
tokio-tungstenite = { version = "0.23", features = ["rustls-tls-webpki-roots"] }
argon2 = "0.5"
aes-gcm = "0.10"
x25519-dalek = "2"
keyring = "2"
```

### 11.2 ID Strategy

The Tauri app uses sequential integer IDs locally. With sync:
- Local IDs remain as-is (unchanged persistence)
- A sidecar `id_map.json` maps `client_id → server_id` after successful push
- Incoming pull entries that don't match a known `client_id` are inserted with a new local ID; their `client_id` recorded for future dedup

### 11.3 Encryption Boundary

- **Local store** (`history.bin`, `notes.bin`): **plaintext** — unchanged
- **Network layer**: encrypt before push, decrypt after pull
- The capture pipeline (`clipboard_watcher.rs`) is untouched; sync module intercepts after entry is stored

### 11.4 New Tauri Commands
```rust
sync_login(email, password, device_name) → Result<SyncUser>
sync_logout()                            → ()
sync_get_user()                          → Option<SyncUser>
sync_get_status()                        → SyncStatus  // { connected, last_synced_at, pending_count }
sync_now()                               → ()
sync_set_enabled(bool)                   → ()
sync_set_server_url(url)                 → ()
```

### 11.5 Graceful Degradation Contract

- All sync operations run in a separate background Tokio runtime — never block the UI
- Network errors → logged + exponential backoff retry (1s, 2s, 4s, max 60s)
- Auth errors → emit `sync:auth-required` Tauri event → UI prompts re-login
- App behavior is identical with sync disabled or server unreachable

---

## 12. Directory Layout

```
orange-copy-paste-clipboard-backend/
├── src/
│   ├── main.py                  # FastAPI app factory, all routers mounted
│   ├── config.py                # Settings via pydantic-settings + .env
│   ├── database.py              # SQLAlchemy async engine + session factory
│   ├── redis_client.py          # Redis async connection pool
│   ├── dependencies.py          # get_current_user, get_db, get_redis
│   │
│   ├── auth/
│   │   ├── __init__.py
│   │   ├── router.py
│   │   ├── service.py
│   │   ├── models.py            # SQLAlchemy: User, Device
│   │   ├── schemas.py           # Pydantic: RegisterRequest, LoginResponse, ...
│   │   └── jwt.py               # Token creation, validation, revocation
│   │
│   ├── sync/
│   │   ├── __init__.py
│   │   ├── router.py
│   │   ├── service.py           # push, pull, conflict resolution, Redis publish
│   │   ├── models.py            # SyncEntry, SyncCursor
│   │   └── schemas.py
│   │
│   ├── blobs/
│   │   ├── __init__.py
│   │   ├── router.py
│   │   ├── service.py
│   │   ├── models.py            # Blob
│   │   ├── schemas.py
│   │   └── s3.py                # boto3 wrapper, pre-signed URL helpers
│   │
│   ├── groups/
│   │   ├── __init__.py
│   │   ├── router.py
│   │   ├── service.py
│   │   ├── models.py            # Group, GroupMembership
│   │   └── schemas.py
│   │
│   ├── realtime/
│   │   ├── __init__.py
│   │   ├── hub.py               # WebSocket connection registry + Redis bridge
│   │   ├── router.py            # /ws endpoint
│   │   └── pubsub.py            # Redis pub/sub subscription helpers
│   │
│   ├── worker/
│   │   ├── __init__.py
│   │   ├── app.py               # Celery application
│   │   └── tasks/
│   │       ├── email.py
│   │       ├── blob_cleanup.py
│   │       └── notifications.py
│   │
│   └── admin/
│       ├── __init__.py
│       └── router.py            # /internal/healthz, /internal/metrics
│
├── migrations/                  # Alembic migration files
│   └── env.py
├── tests/
│   ├── conftest.py
│   ├── test_auth.py
│   ├── test_sync.py
│   └── test_blobs.py
├── docs/
│   └── ARCHITECTURE.md          # This file
├── .env.example
├── pyproject.toml
├── Dockerfile
└── docker-compose.yml
```

---

## 13. Implementation Sequencing

| Phase | Scope | Done When |
|---|---|---|
| **1 — Auth + Infra** | `config`, `database`, `redis_client`, `auth/`, `admin/`, migrations, docker-compose | `docker compose up` → `POST /auth/login` returns JWT; `/internal/healthz` → 200 |
| **2 — Sync Core** | `sync/` router + service + models, cursor table | Push 5 entries device A, pull from device B token → all 5 received |
| **3 — Realtime** | `realtime/` hub + Redis pub/sub, sync service publishes after write | Push from device A while device B has WS open → device B gets `sync:entry` within 200ms |
| **4 — Blobs** | `blobs/` service + s3.py, MinIO in docker-compose | Upload image entry, retrieve download URL, fetch via pre-signed GET |
| **5 — Groups** | `groups/` CRUD + invite + join + key distribution API | Create group, invite user, join, push group-scoped entry, second member pulls it |
| **6 — Desktop** | `src-tauri/src/sync/` module: client, crypto, WS listener, offline queue, commands, settings UI | Tauri app logs in, copies text, second running instance receives it within 2s; works fully offline |
| **7 — Hardening** | Rate limiting (slowapi), Celery tasks, security headers, load test | Login rate-limited at 5/15min; email delivery works; pull endpoint handles 1k entries |

---

## Appendix A — Security Checklist

- [ ] All routes except `/auth/register`, `/auth/login`, `/auth/verify`, `/internal/healthz` require valid JWT
- [ ] Refresh tokens stored as bcrypt hashes only
- [ ] Login rate-limited: 5 attempts / 15 min per IP (`slowapi`)
- [ ] Device private keys never transmitted to or stored by server
- [ ] Server never stores or logs plaintext content
- [ ] Pre-signed S3 PUT URLs expire in 5 minutes; GET URLs in 1 hour
- [ ] Group key rotation triggered on member removal
- [ ] CORS restricted to `tauri://localhost`
- [ ] All SQL via SQLAlchemy parameterized queries (no string interpolation)
- [ ] `Content-Security-Policy` on any browser-facing endpoints
- [ ] `Secure`, `HttpOnly`, `SameSite=Strict` on any cookies (if used)

## Appendix B — Open Questions / Future Work

1. **Mobile clients** — architecture supports them natively; only the integration layer differs
2. **End-to-end encrypted search** — not possible server-side; would require client-side index (out of scope)
3. **Shared clipboard feed UI** — group entries are pullable now; a dedicated UI view is needed in Phase 6
4. **Webhook delivery** — POST to user-configured URLs on new entry (automation pipelines)
5. **Subscription / billing** — quota enforcement is modeled; Stripe integration gates quota upgrades
6. **Push notifications** — Celery `notifications.py` stub ready; FCM/APNs integration is future work
