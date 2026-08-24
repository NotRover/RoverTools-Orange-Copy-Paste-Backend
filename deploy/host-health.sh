#!/usr/bin/env bash
#
# Host health -> an Uptime Kuma "Push" monitor. Reports what Kuma cannot see by
# itself: disk, memory, load, and whether every compose service is actually running.
# Kuma is an uptime monitor, not a metrics agent, and its container monitors need the
# Docker socket - root-equivalent access for a web-facing service, refused in section 9.
# This pushes the same facts from outside the container instead.
#
# Run by: rovertools-health.timer (every 5 min). Set HEALTH_PUSH_URL in .env to enable;
# without it this exits quietly, so a box with no Kuma is unaffected.
# See docs/DEPLOY.md section 12.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE="docker-compose.prod.yml"
DISK_LIMIT=85   # percent used on / before this reports down
MEM_LIMIT=92    # percent of RAM in use before this reports down

cd "$APP_DIR"

# Same parsing as deploy.sh: tolerate quotes, a stray CR, and Kuma's sample query string.
URL="$(sed -n 's/^HEALTH_PUSH_URL=//p' .env 2>/dev/null | tail -n1 | tr -d '\r\"')"
URL="${URL%%\?*}"
[[ -n "$URL" ]] || exit 0

DISK="$(df --output=pcent / | tail -n1 | tr -dc '0-9')"
MEM="$(free | awk '/^Mem:/ {printf "%d", ($2-$7)/$2*100}')"
LOAD="$(cut -d' ' -f1 /proc/loadavg)"

# Every service the compose file declares should have a running container.
WANT="$(docker compose -f "$COMPOSE" config --services | sort)"
HAVE="$(docker compose -f "$COMPOSE" ps --services --status=running 2>/dev/null | sort)"
MISSING="$(comm -23 <(echo "$WANT") <(echo "$HAVE") | tr '
' ' ' | sed 's/ *$//')"

STATUS=up
REASON=""
if [[ -n "$MISSING" ]]; then STATUS=down; REASON="down: ${MISSING};"; fi
if (( DISK >= DISK_LIMIT )); then STATUS=down; REASON="${REASON} disk ${DISK}% over ${DISK_LIMIT}%;"; fi
if (( MEM >= MEM_LIMIT )); then STATUS=down; REASON="${REASON} mem ${MEM}% over ${MEM_LIMIT}%;"; fi

# The message is what you read at a glance in Kuma, so keep the numbers in it even
# when everything is fine - a green beat that says "disk 78%" is an early warning.
MSG="disk ${DISK}% mem ${MEM}% load ${LOAD} svc $(echo "$HAVE" | grep -c . )/$(echo "$WANT" | grep -c . )"
[[ -n "$REASON" ]] && MSG="${REASON} | ${MSG}"

curl -fsS -m 10 -o /dev/null -G "$URL" \
	--data-urlencode "status=${STATUS}" --data-urlencode "msg=${MSG}" \
	|| echo "health: push failed (non-fatal)"
echo "health: ${STATUS} ${MSG}"
