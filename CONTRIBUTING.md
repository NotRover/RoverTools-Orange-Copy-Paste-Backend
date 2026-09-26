# Contributing to the Orange Copy Paste sync server

This file is for people changing the sync server. What the server is and how to
run it locally is in the [README](README.md). To use the app, see the
[user guide](https://orange-copy-paste-app.pages.dev/docs/) instead.

## Ways to contribute

- Report a bug or request a feature by opening an issue.
- Fix a bug or build a feature via a pull request.
- Improve the docs in `docs/`. Every doc and every message a person reads follows
  the workspace [writing guide](https://github.com/NotRover/RoverTools-Orange-Copy-Paste-App/blob/main/docs/writing-docs.md).

For anything larger than a small fix, open an issue first so we can agree on the
approach.

## Project layout

| Path | What it is |
|------|------------|
| `src/main.py` | App composition and router mounting |
| `src/auth/`, `src/sync/`, `src/spaces/`, `src/blobs/`, `src/settings/` | Domain modules |
| `src/realtime.py` | WebSocket endpoint, in-process hub, Redis pub/sub fan-out |
| `src/version.py` | Single source for API and service versions and route prefixes |
| `migrations/` | Alembic migrations |
| `docs/architecture.md` | The wire contract: routes, payloads, DDL, socket events, crypto envelope |
| `tests/` | pytest suite, one module per domain |

Routers stay thin: HTTP concerns in the route, logic in the domain service.
Never hardcode route prefixes; they come from `src/version.py`.

## Getting set up

Prerequisites:

- Python 3.14+
- [uv](https://docs.astral.sh/uv/) for dependency and environment management
- Docker (optional, for the full local stack)

Install dependencies:

```bash
uv sync
```

Run the API against your own Supabase, Redis, and S3/R2 (copy `.env.example` to
`.env` and fill it in):

```bash
uv run uvicorn src.main:app --reload
```

Or bring up the full local stack:

```bash
docker-compose up
```

## Before you open a pull request

Always run, and fix all errors before submitting:

```bash
uv run ruff check src
uv run ty check src
```

Run the tests for any logic change:

```bash
uv run pytest
```

`ruff` and `ty` are not on the venv PATH, so always run them through `uv run`.

For a docs change, or any text a person reads (emails, web pages, error messages),
run the checklist at the end of the writing guide.

Please also:

- Branch off `main`; keep each pull request scoped to one change.
- Open pull requests as **drafts** until they are ready for review.
- Write clear commit messages: a lowercase `type(scope): subject` line, then a
  few bullets on what changed and why.

## Database migrations

Author and review Alembic migrations freely, but **never apply them against a
real database as part of a pull request**, and never push schema changes without
a maintainer's explicit approval. Migrations are applied as a separate,
deliberate step, not automatically on deploy.

## The wire contract

`docs/architecture.md` is the source of truth for everything that crosses the
wire. A change to a payload, route, socket event, or the crypto envelope means a
change to the client as well, so coordinate both. Update `docs/architecture.md` in
the same pull request as the code that changes the contract.

## License

By contributing, you agree that your contributions are licensed under the
GNU Affero General Public License v3.0, the same license as the project (see
[LICENSE](LICENSE)).
