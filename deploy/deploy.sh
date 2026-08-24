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

# Deploy heartbeat -> an Uptime Kuma "Push" monitor. Optional: set KUMA_PUSH_URL in
# .env to switch it on. This is the dead man's switch for the whole pipeline - a
# successful run (including a quiet no-op poll) pings, and a failure or a timer that
# stopped running pings nothing, so Kuma alerts. A deploy that breaks silently and is
# only noticed by hand is the failure mode this exists to catch.
# tr strips surrounding quotes and a stray CR, so a hand-edited .env still parses.
KUMA_PUSH_URL="$(sed -n 's/^KUMA_PUSH_URL=//p' .env 2>/dev/null | tail -n1 | tr -d '\r\"')"

kuma_ping() {  # kuma_ping <up|down> [message]
	[[ -n "${KUMA_PUSH_URL:-}" ]] || return 0
	curl -fsS -m 10 -o /dev/null -G "$KUMA_PUSH_URL" \
		--data-urlencode "status=$1" --data-urlencode "msg=${2:-OK}" \
		|| echo "deploy: heartbeat ping failed (non-fatal)"
}

# Any non-zero exit from here on reports itself, rather than dying quietly in a log
# nobody reads.
trap 'rc=$?; (( rc != 0 )) && kuma_ping down "deploy failed, exit $rc"; exit $rc' EXIT

git fetch --quiet origin main
LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse origin/main)"
RUNNING="$(docker compose -f "$COMPOSE" ps -q api || true)"

# The common poll result: nothing new and the stack is up. Quiet no-op.
if [[ "$LOCAL" == "$REMOTE" && -n "$RUNNING" && "$FORCE" != "--force" ]]; then
	kuma_ping up "no change ${LOCAL:0:7}"
	exit 0
fi

echo "deploy: ${LOCAL:0:12} -> ${REMOTE:0:12}"
git reset --hard --quiet origin/main

SHA="$(git rev-parse --short HEAD)"
export IMAGE="${IMAGE_REPO}:${SHA}"

# Build locally; compose tags the result as $IMAGE (the service's `image:` field).
docker compose -f "$COMPOSE" build api
docker tag "$IMAGE" "${IMAGE_REPO}:latest"

# Recreate the stack (first deploy brings up caddy + redis too). Recreating the
# single `api` leaves a ~1-3s window with no backend, so a request landing in it
# gets a 502 and open WebSockets drop once. Accepted deliberately: no overlap tool
# (docker-rollout/Swarm), and a Caddy retry was tried and measured not to help.
# See docs/DEPLOY.md section 9.
docker compose -f "$COMPOSE" up -d

# Ship Caddyfile changes too. `up -d` does not recreate caddy when only the
# mounted config changed, so without this a Caddyfile edit sits unapplied until
# something recreates the container (historically: the next reboot). The config
# is mounted as a DIRECTORY (see the compose file) precisely so this reload sees
# the current file - a single-file bind mount pins the container to the inode
# that git replaces on every pull, and the container keeps reading the old one.
# Reload is atomic: an invalid config is rejected and the running one keeps
# serving, so a bad Caddyfile fails the deploy instead of taking the site down.
# -T because there is no TTY under systemd.
if [[ -n "$(docker compose -f "$COMPOSE" ps -q caddy)" ]]; then
	# Validate first so a broken config fails here with a readable error. Reload is
	# atomic regardless (an invalid config is rejected and the old one keeps serving);
	# this just makes the failure obvious instead of a terse reload error.
	docker compose -f "$COMPOSE" exec -T caddy caddy validate --config /etc/caddy/Caddyfile
	docker compose -f "$COMPOSE" exec -T caddy caddy reload --config /etc/caddy/Caddyfile
fi

# Keep the last few tagged images for rollback; drop older ones. Best-effort.
docker images "${IMAGE_REPO}" --format '{{.ID}} {{.Tag}}' \
	| awk '$2 != "latest"' | tail -n +$((KEEP_IMAGES + 1)) | awk '{print $1}' \
	| xargs -r docker rmi -f >/dev/null 2>&1 || true

kuma_ping up "deployed ${SHA}"
echo "deploy: done ${IMAGE}"
