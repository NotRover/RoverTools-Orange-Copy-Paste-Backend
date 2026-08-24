FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN pip install --no-cache-dir uv

WORKDIR /app

# Dependencies as their own cached layer, installed straight from the lockfile so
# a build ships exactly the versions uv.lock names — never a resolver's later pick.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Then the app itself.
COPY . .
RUN uv sync --frozen --no-dev \
 && useradd -m -u 10001 app \
 && chown -R app:app /app

ENV PATH="/app/.venv/bin:$PATH"
USER app

EXPOSE 8000

# The rollout gate reads container health to decide when a new container is ready.
# /internal/healthz returns 200 whenever the process can serve (it reports a degraded
# Redis in the body, not the status code) — the right liveness signal for a swap.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/internal/healthz').status==200 else 1)"

# Honour $PORT so the image runs unchanged wherever one is assigned; falls back to
# 8000 for plain `docker run`. Shell form is required for the variable to expand.
CMD ["sh", "-c", "uvicorn src.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
