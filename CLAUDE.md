# CLAUDE.md - Orange Copy Paste Backend

The cloud-sync API for the Orange Copy Paste desktop app. It is a stateless relay and store
for end-to-end encrypted data: FastAPI + Supabase (Postgres + GoTrue Auth) + Redis + S3/R2,
Python 3.14+, managed with **uv**. It lives in its own repo
(`RoverTools-Orange-Copy-Paste-Backend`), mounted as a submodule of the RoverTools
workspace. The workspace root `CLAUDE.md` applies here too; it holds the copy rules, git
rules and doc ownership.

**Every doc and every user-facing string here follows the workspace writing guide, with no
exceptions:**
[`docs/writing-docs.md`](https://github.com/NotRover/RoverTools-Orange-Copy-Paste-App/blob/main/docs/writing-docs.md)
in the app repo (`../docs/writing-docs.md` when checked out as a submodule). That covers
the README, everything in `docs/`, the HTML pages and emails in `src/web/templates/`,
announcement text, and error messages a person reads. Decide the kind of page before
writing it, and run the guide's "Checklist before merging a doc change" before calling a change done. This repo
does not keep its own copy of the guide.

**Owns:** the wire contract (`docs/architecture.md`), host and deployment
(`docs/DEPLOY.md`), and announcements (`docs/ANNOUNCEMENTS.md`).
**Not here:** client internals (app `docs/architecture.md`), who may do what (workspace
`docs/permissions.md`), and end-user how-to (the website). Link to those homes; never
restate them.

## The contract

`docs/architecture.md` is the readable form of the wire contract. It covers routes,
payloads, DDL, socket events and the crypto envelope. Read it before changing anything
that crosses the wire, and update it in the same commit. A wire change is a two-repo change
(this repo and the app) until you have checked otherwise.

- **Supabase owns identity.** This service verifies tokens and never signs one.
- **The server holds no key that opens anything it stores.** All crypto is client-side.
- **Deletes travel as tombstones.** There is no delete route for entries.
- **Device-scoped routes go through the shared dependencies** in `src/dependencies.py`.
  Never re-parse a token inside a route.

## Layout

- `src/main.py` - app composition and router mounting. `src/version.py` - API/service
  versions and route prefixes; never hardcode `/api/v1`.
- One package per domain: `auth/`, `sync/`, `settings/`, `spaces/`, `blobs/`, `admin/`,
  `announcements/`, `web/`. Routers stay thin, with HTTP concerns in the route and logic in
  the domain service.
- `src/realtime.py` - the WebSocket endpoint, the in-process hub, Redis pub/sub fan-out and
  presence.
- `migrations/versions/` - Alembic revisions. `tests/` - pytest, one module per domain.
- `Dockerfile`, `docker-compose.yml` (dev), `docker-compose.prod.yml`, `caddy/`, `deploy/` -
  the self-hosted VPS stack. See `docs/DEPLOY.md`.

## Migrations: a deploy never migrates

The VPS deploy rebuilds and rolls out the image but runs no Alembic step. A merged revision
has not reached the database until someone applies it, and until then the route that
needs it fails with `relation ... does not exist`. The **Migrate database** workflow
previews pending DDL on any push to `main` that touches `migrations/**`. Applying is a
separate, deliberate dispatch. Write and review migrations freely, but **never run them
against a real database without explicit approval.**

## Commands

Always go through `uv run`, because `ruff` and `ty` are not on the venv PATH.

- Lint: `uv run ruff check src`
- Types: `uv run ty check src`
- Tests: `uv run pytest`
- Run: `uv run uvicorn src.main:app --reload`, or the full stack with `docker-compose up`

## Verification

- Any Python change: `uv run ty check src` and `uv run ruff check src`, with every error
  fixed. Run `uv run pytest` for logic changes. Use `# type: ignore` only for a documented
  third-party-stub false positive.
- Any doc, template, email or user-facing message: the writing guide's "Checklist before merging a doc change",
  with every item passing.
- `uv run` can rewrite `uv.lock`. If the task was not a dependency change, restore it with
  `git checkout -- uv.lock`.

## Git

Work on a dedicated branch in this repo, and open PRs against `main` as drafts. Keep
commits scoped to this repo. Never bundle a backend change with a parent-repo commit, except
for a deliberate submodule-pointer bump. Never push or apply migrations without explicit
approval.
