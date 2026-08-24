# Announcements

How to send a message to users from the server. Nothing sends one automatically
today - the machinery is in place and waiting for a reason to use it.

Design notes (why it is one table, why reads are watermarked, why this is the
one plaintext table) live in [`ARCHITECTURE.md` section 16](ARCHITECTURE.md). This
file is the operator's half: what to type, and what the user ends up seeing.

## What a user sees

The message lands in the app's notification centre - the bell above the account
button - as a row with a title, an optional body, and the day it was sent. It
raises the unread badge. That is the whole surface: there is no popup, no sound,
and nothing that interrupts what the user is doing.

Delivery is doubled so that being offline is not a way to miss one. A running
app gets it over the WebSocket within a second; an app that was closed picks the
same row up on its next refresh. Both paths key on the announcement's id, so a
user who gets both still sees one row.

Once it has been handed to a device, that copy is the user's. Dismissing it is
local, and `DELETE` on the server does not take it back.

## Before you can send anything

`ADMIN_API_KEY` must be set on the service. When it is unset the whole admin
surface answers `503`, which is the intended posture for an environment that
should not be posting announcements.

Every call below wants that key in `X-Admin-Key`. Keep it out of shell history
that other people can read - it is the key to every admin route, not just this
one.

```bash
export ADMIN_KEY='<the value from the service environment>'
export API='https://rovertools-temp.ctx.cl'
```

## Send one to everybody

The common case. `title` is the only required field.

```bash
curl -sS -X POST "$API/internal/v1/admin/announcements" -H "X-Admin-Key: $ADMIN_KEY" -H 'Content-Type: application/json' -d '{"title":"Sync is paused Saturday 02:00-03:00 UTC","body":"Your devices keep working. Anything you copy syncs once it is back."}'
```

The response gives you the id and `"delivered_to": "everyone"`.

## Send one to a single account

Pass the user's uuid - the `id` from `GET /internal/v1/admin/users`. A uuid that
does not exist is rejected with `400`, so a typo fails loudly instead of writing
a row nobody will ever read.

```bash
curl -sS -X POST "$API/internal/v1/admin/announcements" -H "X-Admin-Key: $ADMIN_KEY" -H 'Content-Type: application/json' -d '{"user_id":"<uuid>","title":"Your storage limit is now 500 MB","body":"Nothing to do - your existing images stay where they are."}'
```

## Fields

| Field | Default | Notes |
|-------|---------|-------|
| `title` | required | One line, up to 200 characters. This is what the user reads first, and often all they read. |
| `body` | `""` | Up to 2000 characters. Two short sentences is the shape that fits the panel. |
| `user_id` | null | Null addresses everybody. |
| `kind` | `"announcement"` | Which chip and icon the row gets. See below. |
| `ttl_ms` | null | Milliseconds from now, after which it stops being handed out. |
| `data` | `{}` | Passed to the client untouched. Nothing reads it yet. |

### `kind`

Picks the row's icon and the filter chip it sits under: `announcement`,
`reminder`, `sync_warning`, `space_activity`, `space_invite`. Anything else
falls back to `announcement` rather than being dropped, so the server can start
naming a new kind before every installed build knows about it.

In practice `announcement` is right for a notice from us, and `reminder` for a
nudge about something the user could act on. The other three are what the app
raises for itself; sending one from here will file your message alongside real
space and sync events, which is usually not what you want.

### `ttl_ms`

Use it whenever the message is about a moment. A notice about Saturday's
maintenance is worse than useless the following week, and a device that has been
closed for a fortnight would otherwise be handed a stack of windows that have
all long since passed.

```bash
# Gone after 3 days.
-d '{"title":"...","ttl_ms":259200000}'
```

Without a TTL, a message stops circulating after 30 days regardless: that is the
furthest back a first launch is ever told about.

## Withdraw one

Stops it being served to anyone who has not already been handed it. Devices that
have it keep it - this is the server forgetting, not a recall.

```bash
curl -sS -X DELETE "$API/internal/v1/admin/announcements/<id>" -H "X-Admin-Key: $ADMIN_KEY"
```

If you have posted something wrong, assume some users already have it. Deleting
limits the spread; a correction is a second announcement.

## Writing the copy

These are user-facing strings and the app's copy rules apply in full - see the
No-AI-Slop section in `CLAUDE.md`. Practically:

- ASCII punctuation only. No em dashes, curly quotes, or ellipsis characters.
- Say what happens and what the user should do. "Sync pauses Saturday 02:00-03:00
  UTC. Your devices keep working." beats "We are performing scheduled maintenance
  to improve your experience."
- Name real times and numbers. A window without a time is an anxiety, not a
  notice.
- If there is nothing for the user to do, say so. That sentence is the reason
  most people stop reading and go back to work, which is the goal.

## Checking it worked

There is no admin read endpoint - deliberately, since the interesting question
is what a *user* gets. Sign in as a test account and read its feed:

```bash
curl -sS "$API/api/v1/announcements" -H "Authorization: Bearer <jwt>" -H "X-Device-Id: <device-id>"
```

An empty list from an account that should see the message means one of: it
expired, it was addressed to someone else, or the client asked with a `since`
watermark past it. The watermark is per-device and lives in the client's
`sync_state.json`.

## Not yet built

- **No admin UI.** Posting is a curl call, which is fine for the handful of
  times a year this is the right tool and would not be if it became routine.
- **No scheduling.** An announcement is live the moment it is written. Something
  for a future window has to be posted when the window is near.
- **No targeting beyond one user or everyone.** No "users on version X", no "users
  over quota". Those need a query, and the honest answer is to add it when there
  is a real use for it.
- **No read receipts.** The server knows what it sent, not what was read.
