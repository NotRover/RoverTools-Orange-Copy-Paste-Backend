from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── Database ────────────────────────────────────────────────────────────────
    # Supabase Postgres connection string in prod (Project Settings → Database →
    # Connection string → URI, with the async driver). Local Postgres for dev.
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/clipboard"

    # ── Redis ───────────────────────────────────────────────────────────────────
    # Sole responsibilities: realtime pub/sub fan-out + device presence.
    redis_url: str = "redis://localhost:6379/0"

    # ── Supabase Auth ─────────────────────────────────────────────────────────────
    # We do NOT sign tokens — Supabase Auth issues them and we only verify.
    # Required: asymmetric (ES256/RS256) tokens are verified against this project's
    # JWKS endpoint, which is derived from this URL.
    supabase_url: str = ""  # e.g. https://<project-ref>.supabase.co
    # Legacy symmetric secret (Settings → JWT Keys). Only needed for projects
    # created before 2025-10-01, which still sign with HS256; newer projects are
    # asymmetric by default and leave this blank.
    supabase_jwt_secret: str = ""
    supabase_jwt_audience: str = "authenticated"
    # Server-only key for admin ban/delete via the Supabase Admin API (Settings →
    # API Keys): a secret key (`sb_secret_…`) or the legacy `service_role` key,
    # which Supabase deprecates at the end of 2026. Never ship to clients.
    supabase_service_role_key: str = ""

    # ── Blob storage (S3-compatible: Cloudflare R2 in prod, MinIO in dev) ─────────
    s3_endpoint_url: str = "http://localhost:9000"
    s3_bucket: str = "clipboard-blobs"
    aws_access_key_id: str = "minioadmin"
    aws_secret_access_key: str = "minioadmin"
    aws_region: str = "auto"

    # ── App ─────────────────────────────────────────────────────────────────────
    app_env: str = "development"
    app_cors_origins: str = "tauri://localhost,http://localhost:1420"
    # Origin the human-facing pages are reached at, and the base of every link
    # this service puts in front of a person (space invites, the password-reset
    # redirect). Single source, so pointing a domain at the deployment is an env
    # change rather than a code change. No trailing slash.
    public_base_url: str = "https://rovertools-smart-clipboard-app-backend.onrender.com"
    # Default per-user blob quota. Configurable globally here and per-user via the admin API.
    default_blob_quota_bytes: int = 52_428_800  # 50 MB

    # ── Sync limits ───────────────────────────────────────────────────────────────
    # Ceilings, not tiers: ordinary use is nowhere near any of them. They live
    # here rather than in code so they can be moved without a release.
    #
    # One row's ciphertext. 512 KB is roughly 380 KB of plaintext - longer than
    # any real copied text or note. Images are externalised to blob storage and
    # are bounded by the blob quota instead, so this does not apply to them.
    max_entry_bytes: int = 524_288
    # Rows one account may hold. Sized from a 5 MB per-account budget: a typical
    # row measures about 1.5 KB (fixed columns, base64 ciphertext, metadata, the
    # wrapped content key and five index entries), so 5 MB is ~3,400 rows and
    # this keeps headroom. It bounds bytes only at typical row sizes -
    # `max_entry_bytes` is what stops any single row being large, and 3,000 rows
    # at that ceiling would be far more than 5 MB. A byte-accurate cap needs a
    # running per-account total, which is deliberately not built.
    max_entries_per_user: int = 3_000
    # Entries one push may carry, matching the pull page default (its hard cap
    # is 500). The client pushes one entry per request, so this only ever stops
    # an abusive body.
    max_push_batch: int = 200

    # ── Admin ─────────────────────────────────────────────────────────────────────
    admin_api_key: str = ""  # Required for /internal/* admin endpoints; empty disables them (503)

    # ── Email (sharing invites only; verification/reset are handled by Supabase) ──
    email_provider: str = "brevo"  # "brevo" | "smtp"
    brevo_api_key: str = ""
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_from: str = "Orange Clipboard <noreply@example.com>"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.app_cors_origins.split(",") if o.strip()]


settings = Settings()
