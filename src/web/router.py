"""Human-facing HTML pages.

Everything else this service serves is JSON for the desktop app. These two pages
exist because two links have to survive being pasted into an email or a chat
window, where an `orange://` URL is stripped or ignored:

- ``/join/{code}`` - a space invite.
- ``/reset`` - the target of the password-reset mail Supabase sends.

Both do the same small job: show what the link is for, hand it to the app through
the deep-link scheme, and offer a download when the app is not installed. Neither
touches the database, so they paint on the first response even when the service is
cold-starting, and ``/join`` cannot be used to probe whether a code exists.

Deliberately **unversioned**. ``version.py`` versions the product API because the
desktop app negotiates a contract with it; a URL a person clicks in an email
cannot be re-versioned later without breaking every link already sent, which is
the same reason the infra probes are unversioned.
"""

import html
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from src.config import settings
from src.spaces import service as spaces_service

router = APIRouter(tags=["web"], include_in_schema=False)

_TEMPLATES = Path(__file__).parent / "templates"

def read_template(name: str) -> str:
    """Load a shipped template by file name.

    Callers read at import time, not per request: these are shipped assets, not
    user content, and re-reading them would add file IO to a path whose whole
    point is being fast. `email.py` uses this for the invite mail, which is not a
    served page but is the same kind of file and lives in the same directory.
    """
    return (_TEMPLATES / name).read_text(encoding="utf-8")


_SHELL = read_template("shell.html")
_JOIN = read_template("join.html")
_RESET = read_template("reset.html")

# The page's own policy. The global one is `default-src 'none'`, which would block
# the inline style and script these pages are built from; this stays as tight as a
# self-contained page can be - no remote anything, no framing, no form posts.
_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    "img-src data:; form-action 'none'; frame-ancestors 'none'"
)


def _render(title: str, body: str) -> HTMLResponse:
    page = _SHELL.replace("__TITLE__", html.escape(title)).replace("__BODY__", body)
    return HTMLResponse(page, headers={"Content-Security-Policy": _CSP})


@router.get("/join/{code}", response_class=HTMLResponse)
async def join_page(code: str) -> HTMLResponse:
    """Space invite landing page.

    Answers 200 for any well-formed code, whether or not a space holds it: the
    lookup would be a way to enumerate codes, and the app reports an unknown code
    far more usefully than this page could.
    """
    normalized = spaces_service.normalize_invite_code(code)
    deep_link = f"orange://join?code={html.escape(normalized, quote=True)}"
    body = _JOIN.replace("__CODE__", html.escape(spaces_service.format_invite_code(normalized))).replace(
        "__DEEP_LINK__", deep_link
    )
    return _render("Join a space in Orange Copy Paste", body)


@router.get("/reset", response_class=HTMLResponse)
async def reset_page(code: str = "") -> HTMLResponse:
    """Password-reset landing page - the ``redirect_to`` of the recovery mail.

    The reset itself happens in the app, because a new password has to re-wrap the
    account's encryption key and the server never has that key. All this page does
    is carry the one-time code across.
    """
    state = "ok" if code else "fail"
    deep_link = f"orange://reset?code={html.escape(code, quote=True)}" if code else ""
    body = _RESET.replace("__STATE__", state).replace("__DEEP_LINK__", deep_link)
    return _render("Set a new Orange Copy Paste password", body)


def join_url(invite_code: str) -> str:
    """The shareable https link for an invite code.

    One place builds these, so pointing a domain at the service is a change to
    ``public_base_url`` and nothing else. Normalizes first, so a caller that
    already holds the dashed display form gets the same URL as one holding the raw
    code.
    """
    code = spaces_service.format_invite_code(spaces_service.normalize_invite_code(invite_code))
    return f"{settings.public_base_url.rstrip('/')}/join/{code}"
