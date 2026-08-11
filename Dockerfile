FROM python:3.14-slim

WORKDIR /app

# Install uv for fast dependency resolution
RUN pip install --no-cache-dir uv

COPY pyproject.toml .
RUN uv pip install --system --no-cache -e .

COPY . .

EXPOSE 8000
# Honour $PORT so the image runs unchanged on platforms that assign one (Render
# defaults to 10000); falls back to 8000 for plain `docker run`. Shell form is
# required for the variable to expand.
CMD ["sh", "-c", "uvicorn src.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
