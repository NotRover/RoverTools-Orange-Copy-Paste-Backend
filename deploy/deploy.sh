#!/usr/bin/env bash
#
# Build-on-box deploy. A systemd timer (rovertools-deploy.timer) runs this on a
# poll of origin/main; on a new commit it rebuilds the image locally and rolls it
# out. No GitHub Actions, no registry — the image never leaves the box.
#
# Layout on the box (wherever you cloned the repo, e.g. ~/app):
#   <checkout>/       git checkout (this repo; read-only deploy key ~/.ssh/id_repo)
#   <checkout>/.env   secrets, git-ignored, mode 600 (survives git reset)
# Run by:  systemd timer, or by hand:  ~/app/deploy/deploy.sh [--force]
#   --force rebuilds and redeploys even when origin/main has not moved.
set -euo pipefail

# Resolve the checkout from this script's own location (deploy/ is one level down),
# so it works whatever user or home the repo lives in.
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE="docker-compose.prod.yml"
IMAGE_REPO="rovertools-api"
KEEP_IMAGES=5
FORCE="${1:-}"

# Pull with the read-only deploy key by default (manual runs and the timer alike),
# unless the caller already set GIT_SSH_COMMAND.
: "${GIT_SSH_COMMAND:=ssh -i $HOME/.ssh/id_repo -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new}"
export GIT_SSH_COMMAND

# One deploy at a time — a build can outlast the poll interval.
exec 9>/tmp/rovertools-deploy.lock
flock -n 9 || { echo "deploy: another run in progress, skipping"; exit 0; }

cd "$APP_DIR"

git fetch --quiet origin main
LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse origin/main)"
RUNNING="$(docker compose -f "$COMPOSE" ps -q api || true)"

# The common poll result: nothing new and the stack is up. Quiet no-op.
if [[ "$LOCAL" == "$REMOTE" && -n "$RUNNING" && "$FORCE" != "--force" ]]; then
	exit 0
fi

echo "deploy: ${LOCAL:0:12} -> ${REMOTE:0:12}"
git reset --hard --quiet origin/main

SHA="$(git rev-parse --short HEAD)"
export IMAGE="${IMAGE_REPO}:${SHA}"

# Build locally; compose tags the result as $IMAGE (the service's `image:` field).
docker compose -f "$COMPOSE" build api
docker tag "$IMAGE" "${IMAGE_REPO}:latest"

if [[ -n "$RUNNING" ]] && docker rollout --help >/dev/null 2>&1; then
	docker rollout -f "$COMPOSE" api          # start-first swap (new up + healthy, then old out)
else
	# First deploy (brings up caddy + redis too), or the rollout plugin is absent
	# (plain recreate is a few-second HTTP blip; WebSockets reconnect regardless).
	docker compose -f "$COMPOSE" up -d
fi

# Keep the last few tagged images for rollback; drop older ones. Best-effort.
docker images "${IMAGE_REPO}" --format '{{.ID}} {{.Tag}}' \
	| awk '$2 != "latest"' | tail -n +$((KEEP_IMAGES + 1)) | awk '{print $1}' \
	| xargs -r docker rmi -f >/dev/null 2>&1 || true

echo "deploy: done ${IMAGE}"
