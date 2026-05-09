# Backend Implementation TODO

Track implementation status per architecture phase.

---

## Phase 1 — Auth + Infra ✅ COMPLETE

- [x] `src/config.py`
- [x] `src/database.py`
- [x] `src/redis_client.py`
- [x] `src/dependencies.py`
- [x] `src/auth/models.py` — User, Device
- [x] `src/auth/schemas.py`
- [x] `src/auth/service.py`
- [x] `src/auth/jwt.py` — create/decode tokens, get_jwks() with lru_cache
- [x] `src/auth/router.py`
- [x] `src/admin/router.py` — /internal/healthz
- [x] `migrations/versions/0001_initial_auth.py`
- [x] `docker-compose.yml`
- [x] `Dockerfile`

---

## Phase 2 — Sync Core ✅ COMPLETE

- [x] `src/sync/models.py` — SyncEntry, SyncCursor
- [x] `src/sync/schemas.py`
- [x] `src/sync/service.py` — push, pull, conflict resolution
- [x] `src/sync/router.py` — POST /push, GET /pull, POST /cursor, GET /status
- [x] `src/settings/models.py` — UserSettings
- [x] `src/settings/schemas.py`
- [x] `src/settings/service.py`
- [x] `src/settings/router.py` — GET /settings, PUT /settings
- [x] `migrations/versions/0002_sync.py` — sync_entries, sync_cursors, user_settings

---

## Phase 3 — Realtime ✅ COMPLETE

- [x] `src/realtime/hub.py` — in-memory WebSocket registry
- [x] `src/realtime/pubsub.py` — Redis pub/sub bridge + all publish helpers
- [x] `src/realtime/router.py` — /ws WebSocket endpoint with presence TTL

---

## Phase 4 — Blobs ✅ COMPLETE

- [x] `src/blobs/models.py` — Blob
- [x] `src/blobs/s3.py` — boto3 wrapper, pre-signed URL helpers
- [x] `src/blobs/schemas.py`
- [x] `src/blobs/service.py`
- [x] `src/blobs/router.py` — request-upload, confirm-upload, download-url, quota
- [x] `migrations/versions/0003_blobs.py`

---

## Phase 5 — Groups ✅ COMPLETE

- [x] `src/groups/models.py` — Group, GroupMembership
- [x] `src/groups/schemas.py`
- [x] `src/groups/service.py` — join_group returns (JoinResponse, Group) for WS dispatch
- [x] `src/groups/router.py` — CRUD, invite, join (sharing:accepted vs group:membership_changed), key distribution
- [x] `migrations/versions/0004_groups.py`

---

## Phase 6 — Desktop Integration

(Backend-side API is complete; Rust/Tauri client module is out of scope for this repo)

- [x] Settings sync endpoints (GET/PUT /settings, settings:updated WS event) — done in Phase 2

---

## Phase 7 — Hardening ✅ COMPLETE

- [x] Rate limiting via `slowapi` — login (10/min, 30/hr), password-reset/request (5/15min), resend-verification (3/hr)
- [x] `src/limiter.py` — global Limiter singleton wired into app.state
- [x] `src/middleware.py` — SecurityHeadersMiddleware (X-Content-Type-Options, X-Frame-Options, X-XSS-Protection, HSTS, **Content-Security-Policy**)
- [x] `src/email/` — swappable email provider abstraction
  - [x] `base.py` — EmailProvider protocol
  - [x] `brevo.py` — Brevo REST API via httpx (primary, swap via EMAIL_PROVIDER=brevo)
  - [x] `smtp.py` — stdlib smtplib fallback (EMAIL_PROVIDER=smtp)
  - [x] `factory.py` — get_provider() singleton factory
  - [x] `templates.py` — verification + password-reset + **sharing-invite** email templates
- [x] `src/worker/tasks/email.py` — send_verification_email, send_password_reset_email, **send_sharing_invite_email**
- [x] `src/worker/tasks/blob_cleanup.py` — orphan blob cleanup (unconfirmed > 1h)
- [x] `src/worker/tasks/notifications.py` — **detect_offline_devices** Celery beat task (60s interval); SCANs presence:* keys, evicts stale devices, publishes device:offline
- [x] `src/worker/app.py` — beat_schedule includes detect-offline-devices (60s)
- [x] `POST /auth/verify-email` — token validation, marks user.email_verified = true
- [x] `POST /auth/resend-verification` — regenerates token, re-queues email (rate-limited 3/hr)
- [x] `POST /auth/password-reset/request` + `/confirm` — full Redis-token flow, timing-safe responses

---

## Phase 8 — Sharing ✅ COMPLETE

- [x] `src/sharing/__init__.py`
- [x] `src/sharing/schemas.py`
- [x] `src/sharing/service.py`
  - [x] `create_invite()` returns `(InviteResponse, User | None)` — invitee lookup for WS notify
  - [x] `leave_session()` returns `leaving_scope: str` — for scope_changed broadcast
- [x] `src/sharing/router.py`
  - [x] `POST /sharing/invite` — publishes `sharing:invite` WS event + queues sharing invite email
  - [x] `DELETE /sessions/{id}/leave` — publishes `sharing:scope_changed` (was incorrectly `group:membership_changed`)

---

## Well-Known / JWKS ✅ COMPLETE

- [x] `src/well_known.py` — GET /.well-known/jwks.json, Cache-Control: public max-age=3600
- [x] `src/main.py` — mounts well_known_router at root (no prefix)

---

## Tests ✅ COMPLETE

- [x] `pyproject.toml` — added `fakeredis>=2.23` to dev deps
- [x] `tests/conftest.py` — RSA keypair generation, test engine with savepoint isolation, FakeRedis, dep overrides, Celery mock, auth_headers fixture
- [x] `tests/test_auth.py` — register, duplicate email, verify email, login, refresh rotation, device list, password reset flow, JWKS, healthz
- [x] `tests/test_sync.py` — push single/batch, LWW reject older, tombstone wins, pull empty/with entries, cursor pagination, status, update cursor
- [x] `tests/test_blobs.py` — request-upload reachability, quota

---

## Phase 9 — Admin ✅ COMPLETE

- [x] `src/config.py` — added `admin_api_key: str = ""` (env: ADMIN_API_KEY)
- [x] `src/auth/models.py` — added `suspended_at: BigInteger | None` to User
- [x] `src/auth/service.py` — login rejects suspended users with 403
- [x] `src/realtime/hub.py` — added `connection_count()` for metrics
- [x] `src/admin/schemas.py` — UserAdminSummary, UserAdminDetail, UserListResponse, StatsResponse, QuotaUpdateRequest, SuspendRequest
- [x] `src/admin/service.py` — get_stats, list_users, get_user, update_quota, set_suspended, delete_user
- [x] `src/admin/router.py` — full rewrite:
  - [x] `GET /internal/healthz` — public, DB + Redis liveness
  - [x] `GET /internal/metrics` — Prometheus text/plain (12 gauges)
  - [x] `GET /internal/stats` — JSON aggregate stats
  - [x] `GET /internal/admin/users` — paginated list with search
  - [x] `GET /internal/admin/users/{id}` — full user detail
  - [x] `PATCH /internal/admin/users/{id}/quota` — update blob quota
  - [x] `POST /internal/admin/users/{id}/suspend` — suspend / unsuspend
  - [x] `DELETE /internal/admin/users/{id}` — hard delete
- [x] `migrations/versions/0005_admin.py` — ADD COLUMN users.suspended_at
- [x] `docs/ARCHITECTURE.md` — documented all admin endpoints, auth scheme, metrics, and suspension semantics

---

## Cross-Cutting Updates ✅ COMPLETE

- [x] `migrations/env.py` — imports all new models
- [x] `src/main.py` — includes settings, sharing, and well_known routers
