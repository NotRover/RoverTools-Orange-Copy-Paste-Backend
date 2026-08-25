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

# One deploy at a time — a build can outlast the poll interval. Testing fd 9 rather
# than assuming: an open fd survives exec, so after the re-exec below this process
# already holds the lock and must not release and re-race for it. If it somehow did
# not survive, this re-acquires instead of running unlocked.
if [[ ! -e /proc/self/fd/9 ]]; then
	exec 9>/tmp/rovertools-deploy.lock
	flock -n 9 || { echo "deploy: another run in progress, skipping"; exit 0; }
fi

cd "$APP_DIR"

# Our own fingerprint, taken before the pull can replace the file underneath us.
SELF="$APP_DIR/deploy/deploy.sh"
SELF_HASH="$(sha256sum "$SELF" | cut -d' ' -f1)"
BOX="$(hostname)"

# Deploy notifications, to the same Discord channel Netdata alarms use. The deploy
# reports on itself rather than relying on something else noticing: a oneshot unit
# that is inactive between runs is a weak thing to infer health from, and a pipeline
# that breaks quietly is the failure mode that cost us most this month (section 15).
# Only a real deploy or a failure sends - the ~90s no-op polls stay silent.
# The webhook is optional: no value means deploys stay silent, never that they fail.
# Trailing whitespace is stripped because a CR from an editor would corrupt the URL.
ALERT_DISCORD_WEBHOOK="$(sed -n 's/^ALERT_DISCORD_WEBHOOK=//p' .env 2>/dev/null | tail -n1 | tr -d '"' | sed 's/[[:space:]]*$//')"

# Which step we are on, so a failure can say where it died instead of just that it did.
STAGE="startup"

# Wall-clock start and the pre-pull commit, both carried across the re-exec below.
# Without DEPLOY_PREV the re-exec'd process compares HEAD against itself and reports
# an empty commit list - which is what made the first notifications say nothing.
T0="${DEPLOY_T0:-$(date +%s)}"
export DEPLOY_T0="$T0"

# Discord embed fields, accumulated as name/value pairs. Unit and record separators
# rather than any printable delimiter, so a commit subject can contain anything.
FIELDS=""
field() { FIELDS="${FIELDS}${1}"$''"${2}"$''; }

# The JSON is built by python3 reading environment variables, not by string-pasting in
# bash. Commit subjects contain quotes, backslashes and non-ASCII; a hand-rolled shell
# escaper gets one of those wrong eventually and the webhook silently 400s. python3 is
# present on Ubuntu by default; if it ever is not, say so rather than dying inside the
# error handler.
notify() {  # notify <colour> <title> <description>   (fields come from $FIELDS)
	[[ -n "${ALERT_DISCORD_WEBHOOK:-}" ]] || return 0
	if ! command -v python3 >/dev/null 2>&1; then
		echo "deploy: python3 missing, no notification sent" >&2
		return 0
	fi
	local body
	body="$(NF_COLOR="$1" NF_TITLE="$2" NF_DESC="$3" NF_FIELDS="$FIELDS" NF_BOX="$BOX" python3 -c '
import json, os
fields = []
for chunk in os.environ.get("NF_FIELDS", "").split(""):
    if not chunk.strip():
        continue
    name, _, value = chunk.partition("")
    fields.append({"name": name, "value": value or "-", "inline": len(value) < 40})
embed = {
    "title": os.environ["NF_TITLE"][:256],
    "color": int(os.environ["NF_COLOR"]),
    "footer": {"text": os.environ["NF_BOX"]},
}
desc = os.environ.get("NF_DESC", "")
if desc:
    embed["description"] = desc[:4000]
if fields:
    embed["fields"] = fields[:25]
print(json.dumps({"username": "deploy", "embeds": [embed]}))
')"
	curl -sS -m 10 -o /dev/null -X POST -H 'Content-Type: application/json' -d "$body" "$ALERT_DISCORD_WEBHOOK" </dev/null || echo 'deploy: notify failed (non-fatal)'
}

trap 'rc=$?; if (( rc != 0 )); then FIELDS=""; field "Failed at" "$STAGE"; field "Exit code" "$rc"; field "Commit" "$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"; field "Look here" "journalctl -u rovertools-deploy.service -n 50"; notify 15158332 "Deploy FAILED on ${BOX}" "The stack is untouched or half-rolled; check before assuming either."; fi; exit $rc' EXIT

# Nothing pings a monitor here: the notify() calls above are the report. A oneshot
# unit is idle by design, so its systemd state cannot distinguish a healthy pipeline
# from a stopped timer (section 15).
STAGE="git fetch"
git fetch --quiet origin main
LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse origin/main)"
PREV="${DEPLOY_PREV:-$LOCAL}"
RUNNING="$(docker compose -f "$COMPOSE" ps -q api || true)"

# The common poll result: nothing new and the stack is up. Quiet no-op.
if [[ "$LOCAL" == "$REMOTE" && -n "$RUNNING" && "$FORCE" != "--force" ]]; then
	exit 0
fi

echo "deploy: ${LOCAL:0:12} -> ${REMOTE:0:12}"
git reset --hard --quiet origin/main

# Bash reads this script from the handle it opened at startup, so the copy executing
# right now is the PRE-pull one. Without this, a change to deploy.sh takes effect only
# on the NEXT poll - and worse, a step added here does nothing on the very deploy that
# introduced it, silently. That cost three debugging rounds; see section 15.
# --force because the reset already moved HEAD, so a fresh run would find no diff and
# quietly no-op. DEPLOY_REEXEC guards against looping if the hash somehow keeps moving.
if [[ -z "${DEPLOY_REEXEC:-}" && "$SELF_HASH" != "$(sha256sum "$SELF" | cut -d' ' -f1)" ]]; then
	echo "deploy: deploy.sh changed, re-running the updated script"
	export DEPLOY_REEXEC=1
	export DEPLOY_PREV="$PREV"
	exec bash "$SELF" --force
fi

SHA="$(git rev-parse --short HEAD)"
export IMAGE="${IMAGE_REPO}:${SHA}"

# Build locally; compose tags the result as $IMAGE (the service's `image:` field).
STAGE="docker build"
docker compose -f "$COMPOSE" build api
docker tag "$IMAGE" "${IMAGE_REPO}:latest"

# Recreate the stack (first deploy brings up caddy + redis too). Recreating the
# single `api` leaves a ~1-3s window with no backend, so a request landing in it
# gets a 502 and open WebSockets drop once. Accepted deliberately: no overlap tool
# (docker-rollout/Swarm), and a Caddy retry was tried and measured not to help.
# See docs/DEPLOY.md section 9.
STAGE="container rollout"
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
CADDY_STATE="skipped"
STAGE="caddy reload"
if [[ -n "$(docker compose -f "$COMPOSE" ps -q caddy)" ]]; then
	# Validate first so a broken config fails here with a readable error. Reload is
	# atomic regardless (an invalid config is rejected and the old one keeps serving);
	# this just makes the failure obvious instead of a terse reload error.
	docker compose -f "$COMPOSE" exec -T caddy caddy validate --config /etc/caddy/Caddyfile
	docker compose -f "$COMPOSE" exec -T caddy caddy reload --config /etc/caddy/Caddyfile
	CADDY_STATE="reloaded"
fi

# Ship Netdata config from the repo. It cannot be bind-mounted: the entrypoint
# copies stock config into /etc/netdata on every start, and a read-only mount under
# that path makes the cp fail and the container crash-loop (section 15). So the
# files live in git under netdata/conf/ (mirroring /etc/netdata/) and the deploy
# installs them, which keeps the whole monitoring setup reproducible from the repo
# rather than retyped into a volume by hand. Secrets stay out: the Discord webhook
# comes from ALERT_DISCORD_WEBHOOK in .env via the container's environment.
# Restart only when something actually changed - netdata does not re-read on its own.
STAGE="netdata config"
if [[ -n "$(docker compose -f "$COMPOSE" ps -q netdata)" ]]; then
	NETDATA_SEEN=0; NETDATA_DIRTY=0
	# fd 3, not stdin. `docker compose exec` reads stdin even with -T, so a loop fed
	# through stdin has its remaining filenames eaten by the first exec - the loop then
	# ends after one iteration, prints nothing and exits 0. That is what shipped the
	# netdata.conf trim as a no-op (section 15). The inner commands get /dev/null too.
	while IFS= read -r CFG <&3; do
		NETDATA_SEEN=$((NETDATA_SEEN + 1))
		DEST="/etc/netdata/${CFG#netdata/conf/}"
		HAVE="$(docker compose -f "$COMPOSE" exec -T netdata cat "$DEST" </dev/null 2>/dev/null || true)"
		if [[ "$HAVE" != "$(cat "$CFG")" ]]; then
			echo "deploy: installing ${DEST}"
			docker compose -f "$COMPOSE" exec -T netdata mkdir -p "$(dirname "$DEST")" </dev/null
			docker compose -f "$COMPOSE" cp "$CFG" "netdata:${DEST}" </dev/null
			NETDATA_DIRTY=$((NETDATA_DIRTY + 1))
		fi
	done 3< <(find netdata/conf -type f -name "*.conf" | sort)
	# Always say what happened. "Nothing printed" has meant "silently did nothing" twice
	# here, so the count is the difference between a no-op and a working no-change run.
	echo "deploy: netdata config ${NETDATA_SEEN} checked, ${NETDATA_DIRTY} updated"
	if (( NETDATA_SEEN == 0 )); then
		echo "deploy: no netdata config found under netdata/conf - refusing to call that fine" >&2
		exit 1
	fi
	if (( NETDATA_DIRTY > 0 )); then
		docker compose -f "$COMPOSE" restart netdata
	fi
fi

# Keep the last few tagged images for rollback; drop older ones. Best-effort.
docker images "${IMAGE_REPO}" --format '{{.ID}} {{.Tag}}' \
	| awk '$2 != "latest"' | tail -n +$((KEEP_IMAGES + 1)) | awk '{print $1}' \
	| xargs -r docker rmi -f >/dev/null 2>&1 || true

STAGE="reporting"

# What actually shipped. An embed saying only "deployed" is what prompted this: the
# useful facts are which commits, which files, whether anything crossed the wire
# contract, and how long the API was being recreated.
LOG="$(git log --no-merges --pretty=format:'%h %s' "${PREV}..HEAD" 2>/dev/null | head -n 8 || true)"
NCOMMITS="$(git rev-list --count "${PREV}..HEAD" 2>/dev/null || echo 0)"
NFILES="$(git diff --name-only "${PREV}..HEAD" 2>/dev/null | wc -l | tr -d ' ')"
MIGRATIONS="$(git diff --name-only "${PREV}..HEAD" -- migrations/ 2>/dev/null | wc -l | tr -d ' ')"
TOOK=$(( $(date +%s) - T0 ))

if [[ -n "$LOG" ]]; then
	DESC="\`\`\`
${LOG}
\`\`\`"
	if [[ "$NCOMMITS" -gt 8 ]]; then
		DESC="${DESC}
...and $((NCOMMITS - 8)) more"
	fi
else
	DESC="Forced redeploy - no new commits, same code rebuilt."
fi

FIELDS=""
field "Image" "${IMAGE}"
field "Commits" "${NCOMMITS} (${PREV:0:7} -> ${SHA})"
field "Files changed" "${NFILES}"
field "Took" "${TOOK}s"
field "Config" "caddy ${CADDY_STATE}, netdata ${NETDATA_SEEN:-0} checked / ${NETDATA_DIRTY:-0} updated"

# A deploy never runs Alembic (see docs/DEPLOY.md). If a revision shipped in this range
# the database is now behind the code, and the symptom is a live route 500ing on a
# missing relation - worth an amber embed rather than a green one nobody rereads.
COLOUR=3066993
if [[ "$MIGRATIONS" -gt 0 ]]; then
	COLOUR=16159744
	field "MIGRATIONS" "${MIGRATIONS} revision file(s) shipped and NOT applied. Run the Migrate database workflow before trusting the new routes."
fi

notify "$COLOUR" "Deployed ${IMAGE}" "$DESC"
echo "deploy: done ${IMAGE}"
