# Orange Clipboard — Backend Architecture

> **Project:** RoverTools Smart Clipboard Backend
> **Status:** Implemented — reflects current source
> **Last updated:** 2026-08-16
> **Stack:** FastAPI + Supabase (Postgres + Auth) + Redis + S3-compatible blob storage

**Owns:** the wire contract. Routes, payloads, DDL, socket events, the crypto envelope,
and every rule the server itself enforces. If it crosses the network, its one true
description is here, and a client doc that disagrees is wrong.
**Not here:** client internals (`orange-copy-paste-clipboard-app-rust/docs/ARCHITECTURE.md`),
who-may-do-what (root `docs/PERMISSIONS.md`), cross-component invariants (root
`docs/ARCHITECTURE.md`). Link to those rather than restating them — one fact, one home.

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
15. [Spaces Design](#15-spaces-design)
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
        │  blobs · spaces+invites │  JWT──► │   • Auth (users, sessions,      │
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
- **FastAPI** owns the app-specific logic: the sync engine, spaces + E2E key
  distribution, blob upload brokering, presence, and its own WebSocket.
- **Redis** does exactly two things: realtime pub/sub fan-out and device presence.
- **R2** (or any S3-compatible store) holds encrypted binary blobs. Vendor-neutral.
- **No Celery / no worker service.** Background jobs run in-process under a Postgres
  advisory lock (see section 10). Email is sent via FastAPI `BackgroundTasks`.

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
  return the KDF salt + both UMK envelopes the client can unwrap to recover its key.
- **Recovery envelope** — the same UMK wrapped a second time, under a key derived
  from a recovery code the user holds instead of a password they remember. Same
  `kdf_salt` as the password envelope, distinct AAD (`umk-recovery-v1` against
  `umk-envelope-v1`), so the two can never be mistaken for one another. The server
  holds an opaque blob it cannot open, exactly like `pw_wrapped_umk`; there is no
  server-decryptable recovery path and there must never be one.
- **Device registration** — each install registers a device row (carries the device
  public key and, later, the wrapped UMK); returns a `device_id` the client sends
  back as `X-Device-Id`.
- **Public-key management** — store the user's X25519 identity key and per-device
  wrapped UMK for multi-device E2E. The identity key is **write-once**: it is derived
  from the UMK, so every device of an account derives the same one and an honest client
  sends the same value forever. A *different* value means the caller holds a bearer
  token rather than the UMK, and accepting it would redirect every future Space Key
  wrap to a key they own — so a change is refused with 409. Device keys stay writable,
  since each only ever opens that device's own copy of the UMK.

The backend never issues tokens; it only **verifies** the Supabase JWT (HS256,
project secret) on protected routes.

### 2.2 Sync (`src/sync/`)

Owns `sync_entries` (clipboard + notes) and per-device `sync_cursors`.

- Delta push (batches of new/mutated entries) with last-write-wins + tombstones.
- Delta pull since a cursor, covering the caller's own entries **plus** anything
  shared into a space they belong to (subject to that membership's history floor).
- Publishes `sync:entry` to Redis after a write — once to the author's `user:` channel
  and once to each `space:` channel named in the entry's `space_ids`. There is no
  `sync:delete`: a tombstone is a normal `sync:entry` with `deleted_at` set.
- Carries two server-visible routing/key columns it never interprets:
  `space_ids` (fan-out targets) and `wrapped_keys` (the per-entry CEK envelope, section 7.2).

### 2.3 Settings (`src/settings/`)

One encrypted blob per user (`user_settings`), last-write-wins by `updated_at`. On a
client-wins write, publishes `settings:updated` so other devices pull.

### 2.4 Blobs (`src/blobs/`)

Brokers direct-to-object-store uploads.

- `request-upload` → presigned PUT URL + `blob_key` (server never buffers bytes).
- `confirm-upload` → marks the blob confirmed. **This is where the meter starts:** a
  confirmed blob counts against the quota whether or not an entry references it yet.
- `release` → un-confirms a blob whose entry never landed, giving the bytes straight
  back. The client uploads and confirms *before* it pushes the entry, so a push that
  the server refuses (or that fails locally after the upload) leaves an object nothing
  points at. Without this the only collector is the 7-day unreferenced sweep, and a
  retry loop mints a fresh object per attempt. Refuses with 409 while a live entry
  still references the key, so it can never take an image away from an entry using it;
  404 if the key is not the caller's; a no-op (204) if it is already unconfirmed.
- `{blob_key}/download-url` → presigned GET URL.
- `quota` → usage (computed on demand: `SUM(size_bytes)` over confirmed blobs) and
  the per-user quota, plus the two sync ceilings the client cannot see on its own
  (`entry_count` / `entry_limit`, and `max_entry_bytes`; section 6.3).
- **5 MB per-entry hard cap**; per-user quota default **50 MB** (configurable
  globally and per-user via the admin API).

**Every byte is charged to whoever uploaded it, and to nobody else.** `_used_bytes`
sums `blobs` rows `WHERE user_id = <caller> AND confirmed`, and a `blobs` row is only
ever created by `request-upload`, for the uploader. Receiving a shared image creates
no row: `download-url` hands the reader a presigned GET on the *owner's* key
(`{owner_id}/{hex}`) after `_shares_space_with_blob` confirms a live entry carries it
into a space they belong to, and answers 404 rather than 403 to everyone else so the
key's existence is not confirmed. One bucket, namespaced by owner - not a bucket per
user, and never a copy per reader.

The consequence, which is deliberate: **the owner deleting the entry breaks it for
every member.** `release_blob` marks the blob unconfirmed (so the quota stops
counting it immediately), the hourly orphan sweep deletes the object, and any member
who had not already fetched it gets a 404. The bytes were never theirs to keep.

### 2.5 Spaces (`src/spaces/`)

A **space** is the single sharing primitive: a named, persistent, realtime room whose
entries are encrypted under a Space Key the server never sees. There is no space *type*,
no member cap, and no server-side scope. See section 15.

- **Spaces** (`router.py` / `service.py` / `models.py` / `schemas.py`) — create, list,
  get, join by invite code, remove member / leave, delete, and per-member Space Key
  distribution.
- **Addressed invites** (`invites.py`) — one persistent row per (space, invitee email)
  with accept / decline / revoke and live `invite:*` events, alongside the bearer
  invite code. Mounted separately under `/api/v1/invites`. An invite can carry the
  Space Key wrapped for the invitee, which the accept moves onto the new membership.

What flows *into* a space (send filters) and what a receiving client does with an
incoming entry (auto-copy) are client-side choices stored in the encrypted settings
blob. The server only routes ciphertext.

### 2.6 Realtime (`src/realtime.py`)

Single module: the WebSocket endpoint, an in-process connection hub, the Redis
pub/sub bridge, the publish helpers, and device presence. See section 8.

### 2.7 Admin (`src/admin/`)

Ops-only, gated by `X-Admin-Key` (disabled → 503 if `ADMIN_API_KEY` unset), except
the public `/internal/healthz`.

- Health, Prometheus metrics, JSON stats (counts from our tables; online-device
  count from Redis presence; storage computed on demand).
- User management over `profiles` (list/detail/quota). **Account state**
  (email, verification, suspension/ban, deletion) is owned by Supabase and delegated
  to the Supabase Admin API (`src/supabase_admin.py`); those calls require
  `SUPABASE_SERVICE_ROLE_KEY`.

### 2.8 Web pages (`src/web/`)

The only HTML this service serves. Two links have to survive being pasted into an
email or a chat window, where an `orange://` URL is stripped or silently ignored:

- `GET /join/{code}` - a space invite. Shows the code, hands it to the app as
  `orange://join?code=<code>`, and links the latest release when the app is not
  installed.
- `GET /reset` - where the Supabase recovery mail lands. Carries `?code=` across to
  the app as `orange://reset?code=<code>`.

Four decisions worth keeping:

- **Unversioned**, alongside the infra probes (see section 5.0). `version.py` versions
  the product API because the desktop app negotiates a contract with it; a URL a
  person clicks in an email cannot be re-versioned without breaking every link
  already sent.
- **No database call.** `/join` validates the code's shape only
  (`spaces.service.normalize_invite_code`). The service cold-starts on the free tier
  and the page must paint on the first response; a lookup would also confirm to
  anyone whether a given code exists.
- **CSP carve-out.** The global header is `default-src 'none'; connect-src 'self'`,
  which would blank a self-contained page, so `middleware.py` sets it with
  `setdefault` and these routes set their own: inline style and script allowed,
  everything remote still denied. The header is now overridable, never absent.
- **HTML lives in `templates/*.html`**, read once at import and rendered by
  replacing `__PLACEHOLDER__` tokens - no Jinja2 dependency, and the pages stay
  openable and diffable as pages. Every interpolated value goes through
  `html.escape`.

`settings.public_base_url` is the single source for every link this service hands
out, and `web.join_url()` the single builder. Pointing a domain at the service is an
env change.

---

## 3. Tech Stack

| Concern            | Choice                                | Reason                                                           |
| ------------------ | ------------------------------------- | ---------------------------------------------------------------- |
| API framework      | FastAPI + uvicorn                     | Async, Pydantic v2, native WebSocket, auto OpenAPI               |
| Database           | Supabase **Postgres 16**              | Managed; asyncpg + SQLAlchemy 2 async core; RLS available        |
| Identity / Auth    | Supabase **Auth (GoTrue)**            | Managed signup, email verify, password reset, sessions, JWTs     |
| JWT verification   | PyJWT — **ES256/RS256 via JWKS**, legacy HS256 | We verify only; Supabase signs. Asymmetric by default, symmetric accepted when `SUPABASE_JWT_SECRET` is set |
| Cache / realtime   | Redis 7                               | Pub/sub fan-out + device presence (nothing else)                 |
| Blob storage       | Cloudflare **R2** (S3-compatible)     | Direct presigned PUT/GET; zero egress; MinIO for local dev       |
| Background jobs    | in-process asyncio + PG advisory lock | Presence sweep + orphan blob cleanup; no Celery/broker           |
| Email              | Brevo REST (default) or stdlib SMTP   | Space invites only; via `BackgroundTasks`                        |
| KDF (E2E)          | Argon2id (client-side)                | Memory-hard; password → key-wrapping key for the random UMK      |
| Content encryption | AES-256-GCM (client-side)             | AEAD; per-entry content key, server stores ciphertext only       |
| Key exchange       | X25519 (client-side)                  | Multi-device UMK wrapping + Space Key wrapping                   |

**Core Python dependencies** (`pyproject.toml`):

```
fastapi, uvicorn[standard], pydantic, pydantic-settings,
sqlalchemy[asyncio], asyncpg, alembic,
redis[hiredis], boto3, pyjwt[crypto],
python-multipart, httpx, slowapi, email-validator
```

There is **no** `celery`, `python-jose`, or `passlib` dependency. `cryptography`
comes in only as `pyjwt[crypto]`, which PyJWT needs to verify Supabase's asymmetric
(ES256/RS256) tokens — the server verifies tokens it never signs, and every
content-encryption operation is client-side.

---

## 4. Data Models

All server-assigned IDs are UUID v4. Timestamps are `BIGINT` milliseconds since the
Unix epoch (to match the Tauri app's `u64`). `client_id` is stored as `TEXT` and
carries the app's own entry ID — itself a UUID v4 — which is what dedup keys on.

### 4.1 `profiles`

The app-side record for a Supabase Auth user. `id` **equals** the Supabase
`auth.users.id` (the JWT `sub`); linkage is application-level (no cross-schema FK).
Email, password, and verification state live in Supabase — not here.

```sql
CREATE TABLE profiles (
    id               UUID PRIMARY KEY,            -- = auth.users.id
    display_name     TEXT NOT NULL DEFAULT '',
    kdf_salt         TEXT NOT NULL,               -- base64; Argon2id salt for the wrapping key
    identity_pubkey  TEXT,                        -- base64 X25519 public key (E2E)
    pw_wrapped_umk   TEXT,                        -- base64; random UMK wrapped under the KEK
    recovery_wrapped_umk TEXT,                    -- base64; the same UMK wrapped under the recovery code
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
    encrypted_content  TEXT NOT NULL,      -- base64 AES-256-GCM ciphertext (key = the entry's CEK)
    encrypted_metadata TEXT,               -- base64 AES-256-GCM: label/title, pinned, local group names
    created_at         BIGINT NOT NULL,    -- client-originated
    updated_at         BIGINT NOT NULL,    -- client-originated (notes order by this)
    server_ts          BIGINT NOT NULL,    -- server-assigned; the sync cursor
    deleted_at         BIGINT,             -- tombstone (NULL = alive)
    pinned             BOOLEAN NOT NULL DEFAULT false,
    space_ids          UUID[] NOT NULL DEFAULT '{}',   -- server-visible; the fan-out targets
    wrapped_keys       TEXT NOT NULL DEFAULT '{}',     -- CEK envelope; opaque JSON map (section 7.2)
    blob_key           TEXT,               -- object key; NULL for text
    blob_size          BIGINT,
    CONSTRAINT uniq_client_entry UNIQUE (user_id, client_id, entry_type)
);
CREATE INDEX idx_sync_entries_user_ts ON sync_entries(user_id, server_ts);
CREATE INDEX idx_sync_entries_device  ON sync_entries(device_id);
CREATE INDEX idx_sync_entries_spaces  ON sync_entries USING GIN(space_ids);
```

`space_ids` is the only sharing fact the server reads: it decides which `space:`
channels a write fans out to and which memberships can pull the row. `wrapped_keys`
is stored and echoed verbatim — the server cannot tell one wrap from another, and a
row whose `space_ids` is empty is a personal entry. A tombstone keeps the
`space_ids` the entry had, so a delete reaches the same members the entry did.

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

### 4.6 `spaces`

```sql
CREATE TABLE spaces (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id          UUID NOT NULL,      -- profiles.id
    name              TEXT NOT NULL,
    invite_code       TEXT UNIQUE,        -- bearer secret; 8 chars, 72 h TTL
    invite_expires_at BIGINT,
    share_history     BOOLEAN NOT NULL DEFAULT true,  -- may later joiners read older entries?
    members_can_approve BOOLEAN NOT NULL DEFAULT false, -- may members decide join requests?
    key_fingerprint    TEXT,     -- truncated hash of the newest Space Key (section 7.4)
    rekey_requested_at BIGINT,   -- set when a departure demands a new key (section 7.4)
    created_at        BIGINT NOT NULL
);
CREATE INDEX idx_spaces_owner_id ON spaces(owner_id);
```

There is no space *type* and no member cap. `invite_code` is drawn from
`ABCDEFGHJKMNPQRSTUVWXYZ23456789` (no I/L/O/0/1) so it survives being read aloud or
retyped; the API normalizes case and strips `-`/spaces on join, and displays it as
`KX7Q-2M4X`. Anything longer than 8 characters is treated as a legacy
`token_urlsafe` code and matched case-sensitively.

Redeeming a code no longer joins the space. It raises a row in
`space_join_requests` (section 4.10) that somebody already inside has to approve, so a
leaked or forwarded code buys a knock rather than a membership. `members_can_approve`
is the only control over who may answer: owner alone by default, or the owner and any
member. Addressed invites (section 4.9) are unaffected - naming someone by email *is*
the approval.

`share_history` is resolved into the joining member's `history_from_ts` **at join
time**, so flipping it later does not retroactively widen what an existing member can
pull.

`key_fingerprint` is written by the owner alone, when it mints a key, and is what lets a
recipient tell a genuine keyring from one a member made up. Any member may hand a key
over (section 7.4), so the server -- which cannot open a wrap -- is no longer the only
thing standing between a newcomer and a wrong key. A hash of 32 random bytes reveals
nothing about the key it names.

`rekey_requested_at` is the rekey signal. It used to be implicit: a departure cleared
every member's wrap, and a client seeing an empty wrap while holding keys knew to mint.
That destroyed the owner's own recovery path, so the flag now carries the request and the
owner's wrap survives (section 7.4).

### 4.7 `space_memberships`

```sql
CREATE TABLE space_memberships (
    space_id           UUID NOT NULL,     -- CASCADE on space delete
    user_id            UUID NOT NULL,
    role               TEXT NOT NULL DEFAULT 'member',  -- 'owner' | 'member'
    wrapped_space_keys TEXT,      -- JSON array of X25519-wrapped Space Keys, newest first (section 7.4)
    wrapped_by         UUID,      -- who wrapped it; NULL means the owner did
    history_from_ts    BIGINT,    -- pull floor; NULL = full history
    joined_at          BIGINT NOT NULL,
    PRIMARY KEY (space_id, user_id)
);
CREATE INDEX idx_sm_user ON space_memberships(user_id);
```

`role` has exactly two values. There is no `share_scope` column: filtering what a
device sends into a space is a client-side setting the server never learns.

`wrapped_space_keys` is a wrapped **keyring**, not a single key — a JSON array,
newest key first, each element an X25519-wrapped copy for this member. It is an array
because a rekey must not make older entries unreadable: previous Space Keys exist
nowhere else, so a member who restarts after a rekey recovers the whole ring and can
still decrypt entries written under earlier keys. `NULL` is meaningful — it is the
signal that this member needs a (re)distribution (section 7.4).

`wrapped_by` names the account whose public key the recipient must compute its shared
secret against. It exists because distribution is no longer owner-only: with several
possible writers the recipient can no longer assume the counterparty. `NULL` keeps
meaning "the owner", so every row written before migration 0017 still opens.

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

> **Migration note (0011, spaces):** `migrations/versions/0011_spaces.py` drops
> `groups` / `group_memberships` / `group_invites` and creates `spaces` /
> `space_memberships` / `space_invites`, renames `sync_entries.group_ids` →
> `space_ids` (rebuilding the GIN index), and adds `sync_entries.wrapped_keys`.
> It is **destructive on purpose**: the old tables are dropped rather than renamed,
> and `sync_entries` is `TRUNCATE`d because pre-CEK ciphertext was encrypted directly
> under the UMK or a Group Key and cannot be read under the envelope. `downgrade()`
> restores the old *shape*, not the data. This migration is **written but not
> applied** — applying it needs explicit approval.

---

### 4.9 `space_invites`

Addressed counterpart to the bearer `invite_code`: one row per
(space, invitee email), so an invitation survives the invitee being offline and
the inviter can see its fate. `profiles.email` (added in migration 0009) is the
lowercased mirror of the Supabase JWT email claim, captured at bootstrap, used to
resolve invitees to user ids locally.

```mermaid
erDiagram
    profiles ||--o{ space_invites : "inviter / invitee (resolved)"
    spaces ||--o{ space_invites : "CASCADE on space delete"
    spaces ||--o{ space_memberships : ""
    profiles ||--o{ space_memberships : ""
    profiles {
        uuid id PK "= Supabase auth.users.id"
        string email "lowercased JWT-claim mirror, nullable"
        text identity_pubkey "base64 X25519"
    }
    space_invites {
        uuid id PK
        uuid space_id FK
        uuid inviter_id
        string invitee_email "lowercased"
        uuid invitee_user_id "NULL until resolved"
        string status "pending | accepted | declined | revoked"
        bigint created_at
        bigint expires_at "judged at read time"
        text wrapped_space_keys "keyring pre-wrapped for the invitee, nullable"
    }
    space_memberships {
        uuid space_id PK
        uuid user_id PK
        text wrapped_space_keys "wrapped keyring JSON array, nullable"
        uuid wrapped_by "who wrapped it; NULL = the owner"
        bigint history_from_ts "pull floor, nullable"
    }
```

---

### 4.10 `space_join_requests`

The anonymous counterpart to `space_invites`. An invite is addressed and starts with
the inviter; a request starts with the joiner, whom nobody named, so it carries no
inviter, no invitee email, and nothing to deliver. Overloading `space_invites` would
leave half its columns null on every row and put a discriminator in every query.

```sql
CREATE TABLE space_join_requests (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    space_id           UUID NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    user_id            UUID NOT NULL,      -- profiles.id of whoever pasted the code
    status             TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | declined
    wrapped_space_keys TEXT,               -- the approver's wrap, handed to the membership
    wrapped_by         UUID,               -- whose public key opens it
    created_at         BIGINT NOT NULL,
    decided_at         BIGINT,
    decided_by         UUID
);
CREATE UNIQUE INDEX ix_join_requests_space_user   ON space_join_requests(space_id, user_id);
CREATE INDEX        ix_join_requests_space_status ON space_join_requests(space_id, status);
CREATE INDEX        ix_join_requests_user         ON space_join_requests(user_id);
```

The unique index is the abuse cap: a leaked code lets strangers knock, and a knock
must not stack. A declined row is kept rather than deleted, because it is both what
stops the same person knocking again on a code they still hold and the only record
anyone has that somebody tried.

One exception to that: an `approved` row whose membership is gone, because the member
left afterwards. Refusing there would lock them out of a code they still hold with no
way back, so `request_join` reopens the spent row instead of refusing or duplicating it.

`wrapped_space_keys` is why approval is worth the round trip. Whoever approves is by
definition online and holding the keyring at that moment - they are the one clicking -
so the approval writes the wrap in the same call and `add_membership` moves it onto the
new membership, exactly as an accepted invite does. The joiner goes from pending
straight to readable. Joining by code used to be instant and then unreadable for as
long as it took somebody's app to notice.

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
  `/api/openapi.json` - all three **off unless `DOCS_ENABLED=true`**, and 404 when
  it is not set. The schema is a complete map of the surface, `/internal` admin
  routes included, so it fails closed: a deployment that configures nothing keeps
  it private, and `.env.example` turns it on for local work. Every route declares a Pydantic `response_model` and a
  docstring (surfaced as OpenAPI summary/description); tags group the surface
  (auth, sync, settings, blobs, spaces, invites, realtime, ops, admin). The WebSocket `/ws`
  contract is documented in section 5.8 (FastAPI does not emit WebSockets into OpenAPI).

### 5.1 Auth Routes

```
POST   /api/v1/auth/bootstrap
       Body: { display_name? }
       Returns: { user_id, kdf_salt, display_name, wrapped_umk?, recovery_wrapped_umk? }
       Idempotent: creates the profile on first call, generates the stable KDF salt,
       and returns it plus both envelopes (null on a brand-new account).
       Call right after Supabase login. A null recovery_wrapped_umk is what makes
       the client ask the user to save a recovery code.

PUT    /api/v1/auth/umk
       Body: { wrapped_umk }     -- random UMK wrapped under the password-derived key
       Stores the envelope on first setup (and on password change). Server holds only
       the wrapped blob, never the key.

PUT    /api/v1/auth/umk/recovery
       Body: { recovery_wrapped_umk }   -- the same UMK wrapped under the recovery code
       Replacing it revokes the previous recovery code, which is what regenerating
       one does. One code is live at a time.

DELETE /api/v1/auth/umk/recovery
       Drops the envelope. An account that starts over with a fresh UMK must clear
       it, or the old code would hand a recovering client a dead key. Idempotent.

POST   /api/v1/auth/devices
       Body: { device_name?, platform?, app_version?, device_pubkey? }
       Returns: 201 { device_id }     -- store and send back as X-Device-Id

GET    /api/v1/auth/devices
       Returns: [{ id, device_name, platform, app_version, last_seen_at }]

DELETE /api/v1/auth/devices/{device_id}         -- soft-revoke; clears wrapped_umk

POST   /api/v1/auth/keys/register               -- requires X-Device-Id
       Body: { identity_pubkey, device_pubkey }  (base64 X25519)
       409 when identity_pubkey differs from the one already stored: it is derived
       from the UMK and never legitimately changes (section 2.1).

GET    /api/v1/auth/umk/device                  -- requires X-Device-Id
       Returns: { wrapped_umk }   -- the UMK wrapped for the calling device;
       the silent session-restore path. 404 when no wrap is stored or the device
       was revoked, which is what makes revocation cut a device off for real.
       That 404 carries X-Wrap-Absent: 1, and the header is part of the contract:
       a client acts on it by ending the session and asking for a password, so it
       must be able to tell this answer from a 404 produced by a proxy, a
       rewritten path, or a deployment older than the route.

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
             blob_key?, blob_size?, space_ids?, wrapped_keys? }] }
     Returns: { accepted: [{ client_id, server_id, server_ts }],
                conflicts: [{ client_id, reason: 'stale_update' | 'not_your_entry'
                              | 'entry_too_large' | 'account_full' }] }
     At most `max_push_batch` entries per call (422 beyond it, before any work).
     space_ids  — fan-out targets; default []. wrapped_keys — the CEK envelope as a
     JSON string, default "{}". Both are stored verbatim and never interpreted.

GET  /api/v1/sync/pull?after_ts=<ts>&limit=200&entry_type=all|clipboard|note
     Returns: { entries: [...], removals: [...], next_cursor: <ts | null> }
     Each entry echoes space_ids + wrapped_keys. Rows come from the caller's own
     user_id OR any space they belong to, per-membership history floor applied.
     removals: [{ space_id, client_id, entry_type, author_id, removed_by, server_ts }]
     Entries that LEFT one of the caller's spaces since their cursor. Same fields
     as the space:entry_removed event, and for the same reason: apply both through
     one path. Necessary because a removal strips the space id from space_ids, and
     the entries query matches on exactly that array - so from that moment the row
     is absent rather than changed, and a device that was offline when it happened
     can learn it no other way. Apply removals BEFORE entries: an entry re-shared
     after a withdrawal carries the newer server_ts, so that order converges and
     the reverse drops a live entry. Same history floor as entries.
     next_cursor is the LOWER of the two streams' truncation points - a watermark
     may only advance past a point both are complete to, or a device steps over
     removals it never received.

POST /api/v1/sync/cursor        Body: { last_server_ts }
```

There is no `GET /sync/status` and no delete route: the cursor is client-held (and
advanced with `POST /sync/cursor`), and a delete is a push with `deleted_at` set.

**`not_your_entry` — one entry, one author.** Rows are keyed
`(user_id, client_id, entry_type)`, so pushing an entry somebody else wrote does not
update their row: it inserts a *second* row carrying the same `client_id`, and both
fan out to the space. Clients collapse the two onto one item, so the practical
result is the author's text and name replaced by whoever pushed last — a real bug
that cost attribution outright (client `docs/BUGFIX_HISTORY.md` #8).

So push refuses to insert a row for a `client_id` another account already holds in a
space this push targets (`_belongs_to_someone_else`). It is the rare rule the server
*can* enforce without reading anything: it is about which account owns a key, not
about what the content says. Enforced here rather than only in the client because
old builds keep running, and every one of them writes through this route.

The space-overlap condition is what keeps it from refusing honest pushes: one person
with two accounts and the same local history holds colliding `client_id`s by
construction. Only when both rows land in the same space is one claiming to be the
other.

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
POST /api/v1/blobs/release                Body: { blob_key }   → 204
                                          (409 while a live entry references it,
                                           404 if it is not the caller's)
GET  /api/v1/blobs/{blob_key}/download-url  Returns: { presigned_get_url, expires_in_seconds }
GET  /api/v1/blobs/quota                    Returns: { used_bytes, quota_bytes,
                                                       entry_count, entry_limit,
                                                       max_entry_bytes }
```

### 5.5 Spaces Routes  (require `X-Device-Id`)

```
POST   /api/v1/spaces                    Body: { name, share_history?: true }
       Returns: 201 { space_id, invite_code }   -- caller becomes owner, full history
GET    /api/v1/spaces                    Returns: [SpaceOut]   -- every space the caller is in
GET    /api/v1/spaces/{space_id}         Returns: SpaceOut      -- 403 if not a member
GET    /api/v1/spaces/my-join-requests   Returns: [{ space_id, space_name, created_at }]
       The caller's own outstanding knocks. A pending request is not a membership, so
       these spaces are absent from GET /spaces and the wait would otherwise be a blank
       screen. Declared *above* /{space_id}, which takes a UUID and would shadow it
       into a 422.
POST   /api/v1/spaces/join               Body: { invite_code }
       → { status: "pending" | "declined", space_name }
       Raises a join request; does not join (section 4.10). The space name is the only
       thing leaked, and the caller needs it to know what they asked for. 410 when the
       code has expired; already a member returns the name and changes nothing;
       knocking twice returns the same pending row rather than stacking one.
GET    /api/v1/spaces/{space_id}/join-requests
       Returns: [{ id, user_id, display_name, avatar_url, identity_pubkey, created_at }]
       Pending rows only, for a caller who may approve. 403 otherwise.
POST   /api/v1/spaces/{space_id}/join-requests/{request_id}/approve   -> 204
       Body: { wrapped_space_keys?: "[...]" } -- same shape as PUT /invites/{id}/key.
       Handed to add_membership, so the joiner can read immediately. 409 if the request
       was already decided.
POST   /api/v1/spaces/{space_id}/join-requests/{request_id}/decline   -> 204
PATCH  /api/v1/spaces/{space_id}         Body: { share_history?, members_can_approve? }
       Owner only. Both fields optional and applied independently, so setting one
       cannot clobber the other.
DELETE /api/v1/spaces/{space_id}/members/{member_user_id}
       Owner removes a member, or a member removes themselves. 400 if the target is
       the owner (delete the space instead). Clears the remaining *non-owner* wraps
       and stamps spaces.rekey_requested_at to ask for a new key (section 7.4).
DELETE /api/v1/spaces/{space_id}         -- owner only; cascades memberships + invites
POST   /api/v1/spaces/{space_id}/invites Body: { email }   -- owner only; see section 5.6
POST   /api/v1/spaces/{space_id}/keys    -- any member holding the keyring
       Body: { wrapped_keyrings: [{ user_id, wrapped_space_keys }], key_fingerprint? }
       wrapped_space_keys is a JSON array string; stored verbatim on the membership,
       alongside wrapped_by = the caller. key_fingerprint is honoured from the space
       owner only, and clears rekey_requested_at once every member holds a wrap.
       space:rekey is published only to members actually written.
```

`SpaceOut`:

```jsonc
{
  "id": "...", "owner_id": "...", "name": "...",
  "invite_code": "KX7Q2M4X", "invite_expires_at": 1234567, "share_history": true,
  "created_at": 1234567,
  "members_can_approve": false,   // the owner's choice about who answers a knock
  "i_can_approve": true,          // derived by service.may_approve -- the one definition
  "pending_join_requests": 0,     // counted only for a caller who may act on it
  "members": [{
    "user_id": "...", "display_name": "...", "avatar_url": null,
    "role": "owner|member", "joined_at": 1234567,
    "identity_pubkey": "base64 X25519 | null",  // null until that member registers keys
    "has_space_key": true,                      // holds a keyring? presence only, never the bytes
    "online": true                              // any device connected; a hint, see below
  }],
  "my_wrapped_space_keys": "[\"...\",\"...\"] | null",  // only ever the caller's own keyring
  "my_wrapped_by": "uuid | null",         // whose public key opens it; null = the owner
  "key_fingerprint": "base64 | null",     // what a received keyring is checked against
  "rekey_requested_at": 1234567           // non-null = this space is waiting for a new key
}
```

`online` is a snapshot and nothing more. It is read from Redis presence at request
time, and when the presence store is unreachable every member reports `false`
rather than the request failing - the rest of the response comes from Postgres
and is still correct. So it may drive what a member list *shows*, and never
whether an action is allowed or whether a device has really gone away. The same
applies to `online` on `GET /auth/devices`, and to the `user:presence` event.

Six fields carry the key-distribution contract. `identity_pubkey` is what a distributor
wraps for. `has_space_key` lets it wrap only for members who need one — without it, every
reconcile would re-distribute to everybody, the server would echo that back as
`space:rekey`, and clients would loop. `my_wrapped_space_keys` is the restart recovery
path, since Space Keys live in client memory only and `space:rekey` is fire-and-forget;
`my_wrapped_by` says whose public key opens it. `key_fingerprint` is what the recipient
checks the unwrapped ring against, and `rekey_requested_at` is the request for a new one.
A member's keyring is never exposed to anyone else.

`i_can_approve` and `pending_join_requests` are derived server-side rather than left
to the client, so the rule about who may approve has exactly one definition
(`service.may_approve`) and a client cannot drift from it. The count comes back as zero
to anybody who could not act on it anyway.

There is no rotate-invite-code route: a space's code is minted once at creation.
Rotating it on approval was considered and dropped - the code is printed on links and
sitting in mailboxes, so rotating it silently breaks every one of them, and approval
already neutralises a leaked code, which is what rotation was for.

### 5.6 Invite Routes (addressed invites)

The bearer `invite_code` is complemented by persistent, per-email invites.
Lifecycle: `pending → accepted | declined` (invitee) `| revoked` (inviter);
expiry (72 h) is judged at read/accept time, no sweeper.

```
POST   /api/v1/spaces/{id}/invites   Body: { email }   -- owner only, requires X-Device-Id
       Returns: 201 { id, space_id, space_name, inviter_id, inviter_name,
                      invitee_email, status, created_at, expires_at,
                      invitee_identity_pubkey, has_space_key }
       The last two are returned to the inviter only, so it can pre-wrap the keyring;
       the copy published to the invitee omits both.
       400 if it's the caller's own email; 409 if already a member.
       The invitee must already have an account -- 404 otherwise -- so a key can
       always be wrapped for them at this point.
       Refreshes an existing pending invite instead of stacking duplicates.
       Publishes invite:received to the invitee when resolvable; emails the code.
GET    /api/v1/invites               Returns: { sent: [...], received: [...] }
       received = pending, unexpired, matched by user id or the token's email claim
       sent = the caller's 50 most recent, any status, so outcomes are visible
POST   /api/v1/invites/{id}/accept   Joins the space immediately -- no approval step,
                                     because naming an email *is* the approval
POST   /api/v1/invites/{id}/decline
DELETE /api/v1/invites/{id}          -- inviter revokes a pending invite; needs X-Device-Id
PUT    /api/v1/invites/{id}/key      Body: { wrapped_space_keys }  -- inviter only, pending only
       Attaches the keyring wrapped for invitee_identity_pubkey. Accept moves it onto
       the new membership (wrapped_by = the inviter) and clears it from the invite, so
       the member can read the space the moment they join with nobody else online.
       Re-inviting drops a stale wrap rather than reusing it.
```

`GET /invites` and the accept / decline routes authenticate on the bearer token alone,
because they match the caller against the token's `email` claim and are not device-scoped;
they do **not** require `X-Device-Id`. Revoke does, since it goes through the shared
device-scoped dependency.

Invitee resolution uses `profiles.email`, a lowercased mirror of the Supabase
JWT email claim captured at bootstrap (section 4.9) — no Admin API round-trip.

```mermaid
sequenceDiagram
    participant O as Owner client
    participant API as Backend
    participant R as Redis pub/sub
    participant I as Invitee client

    O->>API: POST /spaces/{id}/invites {email}
    API->>API: upsert space_invites (pending, 72h TTL)
    alt email matches a profile
        API->>R: user:{invitee} invite:received
        R-->>I: badge + accept/decline banner
    end
    API--)I: invite email with short code (best-effort)
    I->>API: POST /invites/{id}/accept
    API->>API: add membership (history floor), status=accepted
    API->>R: space:{id} + user:{invitee} space:membership_changed
    API->>R: user:{inviter} invite:updated
    R-->>O: wrap Space Key for new member (section 7.4)
```

### 5.7 Internal Routes

Split into **unversioned infra probes** and the **versioned admin API** (see section 5.0).

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
GET  /internal/v1/admin/email       Returns: provider, configured, email_from, *_set flags -- no secret
POST /internal/v1/admin/email/test  Body: { to }  Returns: { sent, provider, error? }
```

Invite delivery is best-effort and its failures are swallowed (see
`email.send_sharing_invite`), so a broken mail config is invisible from the
client. The two email routes are how you tell: the first reports what the
deployment would use and whether its credentials are present, the second sends one
message and returns the real error. Neither returns a key or a password.

Metrics: `orange_users_total`, `orange_devices_total`, `orange_devices_active_total`,
`orange_devices_online`, `orange_sync_entries_total`, `orange_sync_entries_deleted_total`,
`orange_blobs_total`, `orange_blobs_confirmed_total`, `orange_storage_bytes_used`,
`orange_redis_memory_bytes`.

### 5.8 WebSocket Event Protocol

**Connection:** `GET /ws?token=<supabase access token>&device_id=<device_id>`

The connection is subscribed to `user:<user_id>` and every `space:<space_id>` the
user belongs to. The channel set is resolved once at connect time and re-resolved on
demand (see `resubscribe` below).

**Server → Client events:**

```jsonc
{ "event": "sync:entry",   "payload": { ...SyncEntryOut } }   // incl. tombstones (deleted_at set)
{ "event": "device:online",  "payload": { "device_id": "..." } }   // user: channel; own devices
{ "event": "device:offline", "payload": { "device_id": "..." } }
{ "event": "user:presence",  "payload": { "user_id": "...", "online": true } }   // space: channels
{ "event": "space:entry_removed", "payload": { "space_id": "...", "client_id": "...", "entry_type": "clipboard|note", "author_id": "...", "removed_by": "..." } }
{ "event": "space:membership_changed", "payload": { "space_id": "...", "action": "joined|left|deleted", "user_id": "..." } }
{ "event": "space:rekey",  "payload": { "space_id": "...", "wrapped_space_keys": "[...]" } }
{ "event": "space:join_requested", "payload": { "space_id": "...", "request": { ...JoinRequestOut } } }
{ "event": "space:join_decided",   "payload": { "space_id": "...", "decision": "approved|declined" } }
{ "event": "invite:received",      "payload": { ...InviteOut } }
{ "event": "invite:updated",       "payload": { "invite_id": "...", "status": "accepted|declined|revoked", "space_id": "..." } }
{ "event": "settings:updated",     "payload": { "updated_at": 1234567 } }
{ "event": "ping",         "payload": { "server_ts": 1234567 } }
```

Routing rules worth knowing when implementing a client:

- `sync:entry` is published once to `user:{author}` and once per `space:` channel in the
  entry's `space_ids`. Both carry the same payload, and the **origin device is excluded**
  from delivery, so a device never receives its own write back. A socket in a space it
  also authored into can therefore see the same entry twice — dedupe on
  `(client_id, entry_type)`.
- There is no `sync:delete`. A delete arrives as `sync:entry` with `deleted_at` set.
- `device:online` / `device:offline` are per-device and go only to the user's own
  channel. `user:presence` is the per-user fact addressed to that user's spaces, so
  other members' lists stay current without polling REST. `online: true` is published on
  **every** connect rather than only on the offline→online edge: a socket that dies
  without a close leaves its presence key alive for up to `PRESENCE_TTL`, so a client
  reconnecting inside that window looks like it never left and an edge-triggered publish
  would say nothing. `online: false` comes from the clean-close path and, for sockets
  that died without one, from the presence sweeper. Repeats are expected — a client
  drops an update that changes nothing.
- `space:entry_removed` carries both `author_id` and `removed_by`. They are equal when
  the author withdrew their own post and differ when the space owner took it down, and
  that is the only way a member can tell the two apart.
- `space:entry_removed` is the *fast* path, not the guarantee. The durable record is a
  `space_entry_removals` row written in the same transaction that strips the space id,
  returned by `GET /sync/pull` as `removals`. The event may be missed by anyone not
  connected at the time, and pub/sub does not buffer for absent subscribers, so a
  member whose device was closed learns about the withdrawal from the pull instead.
  Losing the event therefore costs latency, not correctness.
- `space:membership_changed` is published to the space channel **and** to the affected
  user's own channel, because the joiner is not on the space channel yet and a removed
  member may already be off it. `action: "deleted"` is published *before* the row is
  deleted, while the channel still has subscribers.
- `space:join_requested` goes to the space channel when `members_can_approve` is set
  and to the owner's own channel when it is not, so the fan-out matches who may act on
  it rather than being filtered client-side. `space:join_decided` is addressed to the
  requester's own channel - they are not on the space channel, and after a decline they
  never will be.
- `space:rekey` is addressed to one member's own channel and is fire-and-forget: nothing
  retries it. A client that was offline recovers the same keyring from
  `SpaceOut.my_wrapped_space_keys` on its next `GET /spaces`.

**Client → Server:** `{ "event": "pong" }` / `{ "event": "ack" }` (either refreshes
the presence TTL), and `{ "event": "resubscribe" }` — re-resolves the socket's
channel set after a membership change, so space fan-out starts (or stops)
without a reconnect.

### 5.9 Web Page Routes (unversioned, no auth)

```
GET /join/{code}          HTML - space invite landing page
GET /reset?code=<code>    HTML - password-reset landing page
```

Both answer 200 for any well-formed input, set their own Content-Security-Policy,
and are excluded from OpenAPI. See section 2.8.

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
       publish sync:entry to user:{author} + space:{id} for each space_id
       → { accepted, conflicts }
Pull:  client → GET /sync/pull?after_ts=<cursor>&limit=200
       server → rows WHERE server_ts > cursor
                AND ( user_id = me
                      OR space_ids && ARRAY[<one space I'm in>]        -- one arm per membership,
                         [AND server_ts >= that membership's history_from_ts] )
                ORDER BY server_ts ASC (+1 for next_cursor)
              + removals WHERE server_ts > cursor
                AND space_id IN (<spaces I'm in>)                      -- same history floor
                ORDER BY server_ts ASC (+1 for its own cursor)
                next_cursor = min(entry cursor, removal cursor)
       repeat until next_cursor = null → POST /sync/cursor
```

The space arm is not redundant with the WebSocket. Without it, an entry another member
pushed while this device was offline would never arrive at all — live fan-out is the
only other delivery path. The history floor is applied per membership, since the same
caller may have full history in one space and post-join-only in another.

`server_ts` is assigned per accepted entry from wall-clock milliseconds, so a batch does
not share one timestamp. Cursor comparisons are strict (`>`) on pull and the cursor
write only moves forward (`POST /sync/cursor` ignores a lower value).

### 6.3 Size and Row Limits

Four ceilings, all in `src/config.py`. The first three are enforced in
`push_entries`; the fourth sits in middleware, in front of it.

| Setting | Default | Refusal |
|---|---|---|
| `max_entry_bytes` | 512 KB | `entry_too_large`, checked before the row lookup |
| `max_entries_per_user` | 3,000 live rows | `account_full` |
| `max_push_batch` | 200 entries | 422 on the request body, before any work |
| `max_request_bytes` | 8 MB | 413, before the body is read at all |

`max_entry_bytes` applies to `encrypted_content` and `encrypted_metadata`
**separately**. Both are ciphertext on the same row, and capping only the first left
the second as a way around it. Separately rather than as a sum because the client
derives its own local limit from this number, and charging metadata against the
content budget would put that derivation a few bytes off and refuse rows that
should have fit.

`max_request_bytes` is the only one of the four that is not about a row. The three
above it are read off an already-parsed body, so by the time any of them runs the
request has been buffered and built into as many as `max_push_batch` models - which
is the cost they cannot prevent, whatever they then decide. It is checked against
`Content-Length` when the client offers one and against the running total either
way, because a chunked request offers nothing. A body that goes over is answered
413 from inside the receive channel and its read is then closed as a disconnect, so
the route never runs; `BodySizeLimitMiddleware` carries the reason that answer
cannot simply be an exception.

`account_full` counts live rows only (`deleted_at IS NULL`) and is checked *after* the
update path, so a full account can still be emptied - a cap that blocks its own
remedy is a cap the user cannot get out from under.

Both per-account limits count rows by `user_id`, which means an entry somebody else
shared into your space is **their** row on **their** account and does not count
against you. Same rule as the storage quota (section 2.4): you are charged for what you
uploaded, nothing else. The account screen shows both as bars beside each other for
that reason.

Note that `max_entry_bytes` bounds `encrypted_content`, which lives in Postgres, not
R2 - it is the only thing that bounds it. A row cap bounds total bytes only at
typical entry sizes.

### 6.4 Offline Operation

Local `history.bin` / `notes.bin` are the source of truth. The sync client pulls on
startup/reconnect, queues pushes while offline, and applies WS events as they arrive.

---

## 7. E2E Encryption Design

The invariant: entry payloads reach the server (and Supabase) as **ciphertext only** —
never plaintext content, note titles, or labels. All key material is generated, wrapped,
and unwrapped on the client. Space *names* are the deliberate exception: `spaces.name` is
plaintext, because an invitee is shown the space name before they join and therefore
before they hold any key that could decrypt it. See section 7.5 for the full visibility list.

### 7.1 User Master Key (UMK) — envelope model

The UMK is a **random 32-byte key**, not derived from the password. The password
only derives a **key-wrapping key (KEK)** that wraps/unwraps the UMK:

```
KEK          = Argon2id(password, kdf_salt, m=65536, t=3, p=4) → 32 bytes
wrapped_umk  = AES-256-GCM(KEK, UMK)          -- the envelope, stored server-side
UMK          = AES-256-GCM-open(KEK, wrapped_umk)   -- recovered on login
```

- **First setup:** the client generates a random UMK, wraps it under the KEK, and
  stores the envelope via `PUT /auth/umk`.
- **Return / new device:** the client fetches `wrapped_umk` from `bootstrap`, derives
  the KEK from the entered password, and unwraps. A GCM auth failure = wrong password.

Decoupling the key from the password means a **password change only re-wraps the
UMK** (one `PUT /auth/umk`) instead of re-encrypting all data. The server holds only
the wrapped envelope; the UMK lives in memory only.

The same random UMK is wrapped three ways, which is what makes it recoverable
without ever being recoverable *by the server*:

| Envelope | Wrapped under | AAD | Stored |
|---|---|---|---|
| Password | `Argon2id(password, kdf_salt)` | `umk-envelope-v1` | `profiles.pw_wrapped_umk` |
| Recovery code | `Argon2id(recovery_code, kdf_salt)` | `umk-recovery-v1` | `profiles.recovery_wrapped_umk` |
| Per device | `x25519(device_priv, device_pub)` | key-wrap, no AAD | `devices.wrapped_umk` |

The first two are account-wide and open on any machine; the third is local to one
install and needs nothing typed. The recovery and password envelopes share
`kdf_salt` deliberately - same salt, different secret - and the distinct AADs are
what stop one being fed to the other.

#### Recovery ladder

What a client tries, cheapest first, when it needs the UMK:

| Step | Needs | Prompt |
|---|---|---|
| In memory | already signed in | none |
| Device wrap | this machine's keychain key | none, silent |
| Password envelope | the password | normal sign-in |
| Recovery envelope | the recovery code | only when the above cannot be used |
| Start over | nothing | explicit, and says plainly that older synced items become unreadable |

**Hard boundary:** every step above needs a secret the user holds. A recovery path
that needs none means the server can decrypt, which ends the end-to-end guarantee.
That option does not exist here and must not be added.

### 7.2 Content Encryption — the per-entry CEK envelope

Content is **not** encrypted under the UMK or under a Space Key. Every entry gets its own
random 32-byte **content encryption key (CEK)**; the content is encrypted exactly once
under it, and the CEK is then wrapped once per reader:

```
CEK          = 32 random bytes                                   -- per entry, never reused
encrypted_content   = base64( nonce(12B) || AES-256-GCM(key=CEK, content, aad=client_id) )
encrypted_metadata  = same construction, same CEK

wrapped_keys = {                                    -- JSON map, stored as TEXT
  "personal":   wrap(UMK,          CEK),            -- always present; the author's own copy
  "<space_id>": wrap(SpaceKey_now, CEK),            -- one per entry in space_ids
  ...
}
where wrap(k, key) = base64( nonce || AES-256-GCM(key=k, base64(key), aad="key-wrap") )
```

This is what makes fan-out cheap and consistent: sharing the same entry into three spaces
adds three 60-odd-byte wraps, not three copies of the ciphertext, and every reader
decrypts byte-identical content. Re-sharing an existing entry into a new space means
re-pushing it with an extra wrap — the ciphertext does not change.

`aad=client_id` binds the ciphertext to its entry, so a row's content cannot be moved onto
a different `client_id` without breaking the GCM tag. The `"key-wrap"` AAD does the same
job for the wraps: a wrapped CEK cannot be replayed as an entry payload or vice versa.

**Decrypt path.** Try `wrapped_keys["personal"]` against the UMK first; if that is absent
or fails, walk the entry's `space_ids` and trial-decrypt `wrapped_keys[space_id]` against
each key in that space's keyring, newest first. An AES-GCM auth failure just means "wrong
key", so trial decryption is safe and no key epoch or version number has to be carried on
the wire. An entry whose CEK unwraps under no held key is skipped, not dropped — a later
`space:rekey` or `GET /spaces` can make it readable.

`encrypted_metadata` encodes `{ groups, pinned, label }` for a clipboard entry and
`{ title, groups, pinned }` for a note, under the same CEK. `groups` here is the app's
*local* entry-grouping feature and has nothing to do with sharing. An entry's space routing
is deliberately *not* in there: the server has to read `space_ids` in the clear to fan out
at all.

### 7.3 Multi-Device UMK Sharing (X25519)

1. New device generates an X25519 keypair; registers it (`device_pubkey`).
2. An existing device computes `shared_secret = X25519(my_priv, new_device_pubkey)`,
   wraps the UMK, and posts it to `POST /auth/devices/{new_device_id}/key-wrap`.
3. The new device derives the same secret and unwraps the UMK. The server stores
   `wrapped_umk` but can never unwrap it (holds no private key).

### 7.4 Space Key Distribution and Rekey

Every space has a **Space Key**: a random 32-byte key held in client memory, **minted** by
the owner and handed over by **any member who holds it**. It never encrypts content
directly — it only wraps per-entry CEKs (section 7.2). A member's copy is wrapped for their
identity key:

```
shared  = X25519(distributor_identity_priv, member_identity_pubkey)
wrapped = wrap(shared, SpaceKey)
```

Because X25519 is symmetric in the pair, the recipient derives the same secret from
`X25519(their_priv, distributor_identity_pubkey)` — which is why
`SpaceOut.members[].identity_pubkey` carries every member's key, and why `my_wrapped_by`
says which of them to use (null = the owner). A distributor also wraps for itself
(`X25519(priv, own_pub)` is a valid secret); that is how a member recovers its ring after a
restart.

Handing the key over was owner-only until migration 0017, and the cost was a real
blockage: a member who joined while the owner's app was closed could neither read the space
nor write to it, for as long as that lasted. Widening it gives up nothing, because every
member already holds the key in memory and could pass it on by other means — the
restriction only ever stopped members who had no intention of leaking. Minting stays
owner-only, so there is still exactly one account deciding what the current key is.

**Verifying a received ring.** The server stores whatever wrap it is handed and cannot
check it. With more than one possible writer, a member could hand a newcomer a key that is
not this space's: the unwrap would succeed and the victim would silently decrypt nothing.
So the owner writes `spaces.key_fingerprint` when it mints, and a recipient checks the
unwrapped ring against it before adopting it. A mismatch is surfaced as an error rather
than retried, since retrying cannot fix a wrong key. A self-write needs no check.

**The keyring.** `space_memberships.wrapped_space_keys` is a JSON *array*, newest key
first, and every distribution sends the whole ring. Older keys must survive a rekey or
entries written under them become permanently unreadable, and they exist nowhere but in
client memory and these wraps.

**Reconcile** runs on login, on WebSocket connect, on `space:rekey`, and after any
membership change. Every member runs it, for each space it is in, given `GET /spaces`:

1. Recover the local ring by unwrapping `my_wrapped_space_keys` against the public key
   `my_wrapped_by` names. Verify it against `key_fingerprint`; on mismatch, refuse and
   report. An empty or absent wrap does **not** clear the in-memory ring.
2. Only the owner mints. An empty ring means a first key; a non-null `rekey_requested_at`
   while the ring is non-empty means a new key **prepended** to it, published with a fresh
   `key_fingerprint`.
3. Wrap the full ring for every member with `has_space_key = false` (or for everyone, if a
   key was just minted) and `POST /spaces/{id}/keys`. Members with no registered
   `identity_pubkey` are skipped and retried on the next reconcile.

Distribution only happens while someone lacks keys, so the `space:rekey` events the server
echoes back cannot drive an endless reconcile loop. A member who cannot yet be helped is
retried on a backoff, and any keyholder coming online is another chance — in a space with
more than one person, somebody nearly always is.

**Rekey on departure.** When a member is removed or leaves, the server deletes the
membership, clears `wrapped_space_keys` for every remaining **non-owner** member, and sets
`spaces.rekey_requested_at`. That is the entire server-side mechanism; the server never
sees a key. The owner's next reconcile prepends a fresh key and redistributes, and entries
pushed from then on wrap their CEK under the new key, which the departed member never
receives. Distribution clears the flag once every remaining member holds a wrap.

The owner's own wrap is deliberately left alone. Clearing it too — which is what happened
before migration 0017 — meant that if the owner's app restarted before redistributing, the
previous ring was gone, because it lived only in memory and in the wrap just deleted.
Everything ever shared in that space became unreadable for everyone, permanently, and any
member could trigger it by leaving.

Revocation is **best-effort by construction**, and the edges are real:

- A member who already pulled or received an entry keeps its plaintext locally. A rekey
  changes what they can read *next*, never what they already read.
- Between the removal and the owner's reconcile, remaining members show
  `has_space_key = false` but keep decrypting with the ring already in memory. Nothing
  breaks; the state is transient.
- A rekey needs the owner online, since only the owner mints. Until then no new key
  exists, and a removed member who still holds the old key could decrypt entries pushed
  under it.
- A wrap is opened against a public key the *server* returned, unpinned. That is fine
  against a passive server, which is the threat model here; an active malicious server
  could substitute a key it owns. The fingerprint check defends against another member,
  not against the server that also serves the fingerprint.

```mermaid
sequenceDiagram
    participant J as Joiner client
    participant API as Backend
    participant R as Redis pub/sub
    participant O as Owner client

    J->>API: POST /spaces/join -> a join request, not a membership
    O->>API: POST .../join-requests/{id}/approve { wrapped_space_keys }
    Note over API: approve and accept both arrive carrying a wrap:<br/>moved onto the membership, so the joiner reads at once
    API->>R: space:{id} + user:{joiner} space:membership_changed
    R-->>O: membership_changed → reconcile
    O->>API: GET /spaces  (joiner: has_space_key=false, identity_pubkey)
    Note over O: any keyholder, not only the owner<br/>wrapped[] = ring.map(k => wrap(X25519(my_priv, joiner_pub), k))
    O->>API: POST /spaces/{id}/keys [{user_id, wrapped_space_keys}]
    API->>API: persist the keyring + wrapped_by on the membership
    API->>R: user:{joiner} space:rekey
    R-->>J: space:rekey → unwrap ring, check key_fingerprint, cache in memory
    Note over J,API: restart: GET /spaces returns my_wrapped_space_keys<br/>+ my_wrapped_by → unwrap the ring again
```

### 7.5 Server Visibility

Server sees: entry type/kind, timestamps, `pinned`, blob keys and sizes, which spaces an
entry was shared into (`space_ids`), space membership (user ↔ space), space names, invitee
emails, and public keys.

The one plaintext exception is `announcements` (section 16) — rows the *server itself*
wrote, so there was never a plaintext of the user's to protect.

Server never sees: `encrypted_content`, `encrypted_metadata`,
`user_settings.encrypted_blob`, the UMK, any CEK, or any Space Key. `wrapped_keys` and
`wrapped_space_keys` pass through as opaque strings — the server stores and echoes them
without parsing, and holds no private key that could open either. Send filters and
auto-copy live in the encrypted settings blob, so the server cannot tell why a given entry
was or was not shared.

---

## 8. Realtime Architecture

`src/realtime.py` is one module: WebSocket endpoint + in-process hub + Redis bridge +
presence + publish helpers.

### 8.1 Fan-out (horizontally scalable)

Each API process keeps an in-memory hub of its **local** sockets and runs one Redis
`psubscribe("user:*", "space:*", "broadcast:*")` listener. A write publishes an event to Redis; every
process forwards it to its own local sockets on that channel. No sticky sessions; add
replicas freely.

Origin-device exclusion travels in-band: the publisher attaches `_origin_device` to the
Redis message, and each process strips that field before delivering and skips sockets whose
`device_id` matches. That is what keeps a device from receiving its own push back as a
`sync:entry` — and it is per-device, not per-user, so the author's *other* devices still get
the event.

### 8.2 Presence (connection-driven)

- **Connect** → `SADD user:{uid}:devices {did}`, `SET presence:{uid}:{did} EX 300`,
  publish `device:online` **immediately**.
- **Clean disconnect** → `SREM`, `DEL presence:{uid}:{did}`, publish `device:offline`
  **immediately**.
- **Heartbeat** — server pings every 25 s; any client message/`pong` refreshes the
  presence TTL.
- **Crash backstop** — a socket that dies without a clean close leaves its presence
  key to expire; the maintenance sweeper (section 10) then emits `device:offline`. So the
  instant path is primary and the sweep is a safety net, not a 60 s-latency primary.

---

## 9. Auth Flow

Identity is Supabase's; the app layers device + key state on top.

```
1. Sign up / log in / verify / reset  ──► Supabase Auth (client SDK / GoTrue REST)
        → client holds a Supabase access token (JWT, sub = user id) + refresh token

2. POST /api/v1/auth/bootstrap { display_name? }   (Authorization: Bearer <JWT>)
        → ensures a profile, returns kdf_salt + wrapped_umk
        → client derives KEK = Argon2id(password, kdf_salt); unwraps UMK from wrapped_umk
          (or, if null, generates a random UMK, wraps it, and PUT /auth/umk)

3. POST /api/v1/auth/devices { device_name, platform, ... }
        → { device_id }  (client stores it, sends X-Device-Id on subsequent calls)

4. All app calls: Authorization: Bearer <JWT> [+ X-Device-Id]
        → FastAPI verifies the JWT (ES256/RS256 via JWKS, or legacy HS256; aud='authenticated')
```

**Token verification** (`src/auth/tokens.py`): the algorithm is read from the token
header and allowlisted to `ES256`/`RS256`/`HS256` before any key is selected, so `none`
and unknown algorithms are refused up front. Asymmetric tokens verify against the
project's JWKS (`{SUPABASE_URL}/auth/v1/.well-known/jwks.json`, cached in-process for
300 s, so Supabase-side rotation needs no redeploy); `HS256` verifies against
`SUPABASE_JWT_SECRET` and is refused outright when that secret is unset. Algorithm
confusion has nothing to forge against, since the two branches draw on unrelated key
material. Decode requires `exp` and `sub` and checks the audience, with 30 s of leeway
for clock drift between Supabase and this host. No deny-list — Supabase owns session
revocation. To immediately cut off a user, ban them via the admin suspend endpoint
(Supabase).

**A verification failure answers 401 only when a token was actually judged.** Every
authenticated route can therefore return one of two statuses on failure, and the
difference is part of the contract:

| Status | Means | What a client may do |
|---|---|---|
| `401` | The token was read and refused: bad signature, unknown `kid` in a key set we *did* fetch, expired, wrong audience, disallowed algorithm | Treat the credential as dead. Re-authenticate. |
| `503` + `Retry-After` | We could not reach the JWKS endpoint, so nothing was judged (`PyJWKClientConnectionError`) | Retry. The credential is untouched. |

Collapsing the second into the first is not a cosmetic error: the desktop client's
silent session restore reads 401 as a verdict and ends the session with no retry, so a
single failed DNS lookup here signs a user out and makes them type a password. An
unknown `kid` fetched successfully stays 401 on purpose — a forged random `kid` must
not be a way to drive 5xx out of the service.

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

Email (space invites) is sent separately via FastAPI `BackgroundTasks` (best-effort;
failures are logged, not surfaced to the request).

Every message is rendered from `src/web/templates/email_shell.html` plus a body
fragment, so the invite and the admin test mail share one frame. Supabase sends
account mail from templates held in its own dashboard, which cannot import from
here: `scripts/render_supabase_emails.py` renders paste-ready copies off the same
shell into `docs/supabase-email/`, and has to be re-run and re-pasted when the
shell changes.

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

Self-hosted on a single VPS in Docker (Caddy + FastAPI + Redis); Supabase and R2 stay
external. The operational steps — hardening, the compose/Caddy stack, CI/CD — live in
[`DEPLOY.md`](DEPLOY.md); this is the model.

- **Supabase** (managed): Postgres + Auth. `DATABASE_URL` (asyncpg driver, via the
  Supavisor pooler), `SUPABASE_URL` and optionally `SUPABASE_JWT_SECRET`.
- **FastAPI**: stateless — run N replicas. Caddy terminates TLS, reverse-proxies, and
  passes the `/ws` upgrade through with no extra config. The image is portable, so it can
  also run on any platform that assigns `$PORT`.
- **Redis**: co-located as a compose service on the internal network (no published port);
  holds only pub/sub fan-out + presence, so persistence is off.
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
2. **Bootstrap for the KDF salt + envelope.** Call `POST /auth/bootstrap` after login
   to get `kdf_salt` and `wrapped_umk`; derive the KEK and unwrap the UMK (or generate
   + `PUT /auth/umk` on first setup).
3. **Register a device, send `X-Device-Id`.** Call `POST /auth/devices` once, persist
   `device_id`, and send it as the `X-Device-Id` header on device-scoped calls.
4. **WebSocket** connects to `/ws?token=<supabase JWT>&device_id=<device_id>`.
5. **Deletion is a tombstone** in `POST /sync/push` (`deleted_at` set) — there is no
   dedicated delete route. Keep the entry's `space_ids` on the tombstone so the delete
   reaches the members who received the entry.
6. **Every push carries a CEK envelope.** Mint a per-entry key, encrypt content and
   metadata under it with `aad=client_id`, and send `wrapped_keys` with a `"personal"` wrap
   plus one wrap per space id in `space_ids` (section 7.2). An entry with an empty envelope is
   accepted by the server and readable by nobody.
7. **Reconcile space keys, don't trust the event.** `space:rekey` is fire-and-forget;
   `GET /spaces` → `my_wrapped_space_keys` is the authoritative recovery path, and the
   whole keyring must be kept so older entries stay readable (section 7.4).

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
│   ├── dependencies.py       # get_current_claims / get_current_user_only / get_current_user_id / get_redis
│   ├── realtime.py           # WS endpoint + hub + Redis bridge + presence + publishers
│   ├── background.py         # advisory-lock maintenance loop (presence sweep, blob cleanup)
│   ├── email.py              # invite + test mail (Brevo | SMTP), via BackgroundTasks
│   ├── web/                  # /join and /reset pages, and the mail templates
│   ├── supabase_admin.py     # Supabase Auth Admin API client (get/ban/delete user)
│   ├── middleware.py         # security headers
│   ├── limiter.py            # slowapi limiter instance
│   ├── auth/                 # tokens.py (JWT verify) + router/service/models/schemas
│   ├── sync/                 # router/service/models/schemas
│   ├── settings/             # router/service/models/schemas
│   ├── blobs/                # router/service/models/schemas + s3.py
│   ├── spaces/               # router/service/models/schemas + invites.py (addressed invites)
│   ├── announcements/        # router/service/models/schemas (server-authored messages)
│   └── admin/                # router/service/schemas
├── migrations/               # Alembic (0001–0012)
├── tests/                    # conftest + test_auth / test_sync / test_blobs / test_spaces_invites / test_announcements
├── docker-compose.yml        # api + db + redis (dev)
├── Dockerfile
├── pyproject.toml
└── .env.example
```

There is no `worker/`, `realtime/` package, `email/` package, `groups/` package,
`groups/sharing.py`, `well_known.py`, or `auth/jwt.py` — those were removed or collapsed.

`tests/test_spaces_invites.py` covers spaces and invites (replacing
`test_groups_invites.py`). **It has not been executed** — `ruff` and `ty` pass, but
`pytest` needs a local Postgres and has not been run against this change.

---

## 14. Client / Backend Contract Drift

No known drift. The Rust client (`src-tauri/src/sync/client.rs`) calls exactly the routes
in sections 5.1–5.6 — auth, sync push/pull/cursor, settings, blobs, `/spaces*`, `/invites*` — and
nothing else. The routes it used to reach for and that do not exist are gone from the
client too: `/auth/login|refresh|logout` (Supabase Auth handles those), a delete route,
`/sharing/*` in any form, and any rotate-invite or scope endpoint.

Three contract points still catch a fresh client and are worth restating:

| Trap | Reality |
| ---- | ------- |
| Deleting an entry | A push with `deleted_at` set. There is no DELETE route, and the tombstone must keep the entry's `space_ids`. |
| Device-scoped calls | Need `X-Device-Id` in addition to the bearer token. `/auth/bootstrap`, `/auth/devices` and `/invites/*` do not. |
| WebSocket | `/ws?token=<supabase JWT>&device_id=<device_id>` — both query params are required, and `device_id` must be a registered device. |

---

## 15. Spaces Design

A space is a named room any number of users can join, where every member sees the entries
other members send in, in real time. It is the **only** sharing primitive: the earlier split
between persistent pool groups and ephemeral Live Share sessions is gone, along with
`group_type`, `max_members`, and server-side `share_scope`.

Why one primitive: the two used the same tables, the same key distribution, and the same
fan-out. The only real differences were a member cap and a scope column the server stored
but never enforced. Both were removed rather than kept as configuration.

### 15.1 Personal Sync vs Spaces

They are separate concerns on the same pipe, distinguished by one field:

- **Personal sync** — an entry with `space_ids = []`. Reaches only the author's own
  devices, over `user:{uid}`. This is the baseline: everything a client captures syncs
  personally, whether or not any space exists.
- **A space** — an entry with one or more `space_ids`. Still reaches the author's own
  devices, *and* every member of each listed space. The same row serves both, because the
  CEK envelope carries a `"personal"` wrap alongside the per-space wraps (section 7.2).

Nothing enters a space implicitly. The client decides per entry, from its own send filters,
and the server has no view into that decision.

### 15.2 Establishing a Space

```
Owner   → POST /api/v1/spaces { name, share_history }
          → { space_id, invite_code }; owner membership with history_from_ts = NULL
Invitee → POST /api/v1/spaces/join { invite_code }            -- bearer secret path
          → a join request; an approver posts .../approve with the wrapped ring
       or POST /api/v1/invites/{id}/accept                    -- addressed path (section 5.6)
          → membership added, history floor resolved from share_history at join time
Owner   → reconciles and posts the wrapped keyring (section 7.4)
```

Two invitation paths, one membership model. The invite code is a bearer secret usable by
anyone holding it; an addressed invite targets one email, survives the invitee being
offline, and reports its outcome back to the inviter.

### 15.3 Live Entry Fan-out

A client sends an entry into a space by listing the space in `space_ids` and adding that
space's wrap to `wrapped_keys`, then pushing as normal. The server stores the row and
publishes `sync:entry` to `user:{author}` and to each `space:{id}`; every member's socket
receives it and unwraps the CEK locally. Members who were offline pick the same row up on
their next `GET /sync/pull` through the space arm (section 6.2), subject to their history floor.

### 15.4 Leaving, Removal, and Deletion

- `DELETE /spaces/{id}/members/{uid}` — the owner removing a member, or a member removing
  themselves. Both clear every remaining member's wrapped keyring, which is what drives the
  rekey (section 7.4). The owner cannot leave their own space.
- `DELETE /spaces/{id}` — owner only. `space:membership_changed` with `action: "deleted"`
  is published first, while the channel still has subscribers; then the row goes and
  memberships and invites cascade.

Deleting a space does not delete its entries. Rows keep their `space_ids`, but with no
memberships left the space arm of the pull query matches nobody, and clients drop keyrings
for spaces that no longer come back from `GET /spaces` (zeroizing the key bytes). What
members already decrypted stays in their local history — that is a deliberate consequence
of client-side storage, not a gap to be closed server-side.

---

## 16. Announcements

Operator's guide - how to actually send one, and how to word it:
[`ANNOUNCEMENTS.md`](ANNOUNCEMENTS.md). Nothing sends one automatically yet; the
machinery is in place and waiting for a reason to use it.

The one table holding plaintext, and deliberately: an announcement is the
*service* talking - a maintenance window, a quota change, a note to one account.
Nothing in it derives from an entry, a note, or a space name, so there is nothing
the server would have had to decrypt in order to write it. Anything that *would*
need decrypting is not an announcement, and belongs in an event the client can
phrase itself.

One table, addressed or not:

| `user_id` | Means | Published to |
|-----------|-------|--------------|
| a uuid | that account only | `user:<id>` |
| null | everybody | `broadcast:all`, which every socket joins on connect |

Delivery is doubled, because the interesting case is a user who is not looking.
The row is written first, then pushed over the socket - so a client with nothing
connected still gets it from `GET /api/v1/announcements`, and one that is
connected does not wait for its next refresh. The client keys both on
`announcement:<id>`, making a double delivery a no-op.

**Reads are watermarked, not marked read.** There is no per-user read state here
- the endpoint answers "what is newer than `since`". That is what lets a client
dismiss a message without the next refresh resurrecting it: it advances its own
watermark past whatever it was handed and never asks for that window again. A
first launch omits `since` and gets the last 30 days, capped at 100 rows newest
first, so a new device arrives with what is current rather than a changelog.

`expires_at` is the other half of that: a notice about Tuesday's maintenance is
worse than useless on Friday, and without a stop date every new device would be
told about every window the service ever had.

Posting is an admin act (`POST /internal/v1/admin/announcements`, `X-Admin-Key`).
`DELETE` stops the row being served; devices already told keep their copy, since
this is the server forgetting rather than a recall.

---

## 17. Security Checklist

- [x] All `/api/v1/*` routes (except `/auth/bootstrap`'s own JWT gate) require a valid
      Supabase JWT; `/internal/*` (except `/healthz`) require `X-Admin-Key`.
- [x] Backend verifies, never signs, tokens (PyJWT; ES256/RS256 via the project JWKS or
      legacy HS256 via `SUPABASE_JWT_SECRET`, audience `authenticated`, `exp` required,
      algorithm allowlisted from the header).
- [x] Session/refresh/verification/reset owned by Supabase Auth; account
      suspension/deletion delegated to the Supabase Admin API.
- [x] Device and identity private keys never leave the client; the server stores only public
      keys and the opaque `wrapped_umk` / `wrapped_space_keys` / `wrapped_keys` blobs.
- [x] Server stores only ciphertext for `encrypted_content`, `encrypted_metadata`,
      `encrypted_blob`; blobs are client-encrypted before upload.
- [x] Content keys are per entry (CEK) with `aad=client_id`, and key wraps use a separate
      `"key-wrap"` AAD, so a wrap cannot be replayed as content or vice versa.
- [x] Blob download URLs are member-scoped: a non-owner gets a presigned GET only when a
      live sync entry carries that blob into a space they belong to (404, not 403, so the
      key's existence is not confirmed).
- [ ] Space Key revocation is **best-effort**: removal clears the server-side keyrings and
      the owner's client rekeys, but a departed member keeps whatever they already
      decrypted, and no new key exists until the owner is next online (section 7.4).
- [x] Presigned R2 PUT URLs expire in 5 min; GET in 1 h.
- [x] Per-entry 5 MB cap + per-user quota (default 50 MB) enforced server-side.
- [x] `SUPABASE_SERVICE_ROLE_KEY` is server-only and never returned to clients.
- [x] Security headers (`X-Content-Type-Options`, `X-Frame-Options`, CSP,
      `Referrer-Policy`, `Permissions-Policy`, conditional HSTS) via middleware.
- [x] All SQL via SQLAlchemy parameterized queries.
- [x] Asymmetric JWKS verification is in place, so Supabase-side key rotation needs no
      redeploy; the shared HS256 secret remains accepted only for legacy projects.
```
