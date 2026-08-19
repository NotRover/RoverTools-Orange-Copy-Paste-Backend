"""Transactional email — sharing invites only.

Account emails (verification, password reset) are sent by Supabase Auth. The one
message the app sends itself is the space invite. Provider is chosen by the
`EMAIL_PROVIDER` setting; both send paths are synchronous and are meant to run
via FastAPI `BackgroundTasks` (which executes them in a threadpool).
"""

import logging
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

import httpx

from src.config import settings
from src.spaces import service as spaces_service
from src.web import router as web

logger = logging.getLogger(__name__)

_BREVO_ENDPOINT = "https://api.brevo.com/v3/smtp/email"

# Mail markup. Real .html files rather than string literals, so they can be opened
# in a browser and diffed as pages - the same arrangement as the pages this
# service serves. One shell wraps every message, so account mail sent by Supabase
# (whose templates are pasted copies of this same frame) and mail sent from here
# look like one product.
_MAIL_SHELL = web.read_template("email_shell.html")
_MAIL_INVITE = web.read_template("email_invite.html")
_MAIL_TEST = web.read_template("email_test.html")

_DEFAULT_FOOTER = "Sent by Orange Copy Paste. You can ignore this message if it was not meant for you."


def _render_mail(heading: str, body: str, footer: str = _DEFAULT_FOOTER) -> str:
    """Wrap a body fragment in the shared frame. Callers escape their own
    interpolations before substituting them into the fragment."""
    return (
        _MAIL_SHELL.replace("__HEADING__", heading)
        .replace("__BODY__", body)
        .replace("__FOOTER__", footer)
    )

# A blocked SMTP port does not refuse the connection, it swallows it, so an
# unbounded `smtplib.SMTP()` parks a threadpool worker until the OS gives up
# (minutes). Fail in seconds instead: a send is best-effort anyway.
_SMTP_TIMEOUT_SECONDS = 15

# One send, up to three tries. Invites are queued in a background task with no
# durable queue behind them, so a dropped message is gone - a couple of retries
# is the cheapest thing that turns a transient blip into a delivered mail.
_BREVO_ATTEMPTS = 3
_BREVO_BACKOFF_SECONDS = 2


def _send_brevo(to_address: str, subject: str, html_body: str, text_body: str) -> None:
    if not settings.brevo_api_key:
        raise RuntimeError("BREVO_API_KEY is not configured")

    parts = settings.email_from.split("<")
    sender_name = parts[0].strip() if len(parts) > 1 else "Orange Clipboard"
    sender_email = parts[-1].rstrip(">").strip()

    payload: dict = {
        "sender": {"name": sender_name, "email": sender_email},
        "to": [{"email": to_address}],
        "subject": subject,
        "htmlContent": html_body,
    }
    if text_body:
        payload["textContent"] = text_body

    headers = {"api-key": settings.brevo_api_key, "Content-Type": "application/json"}
    # Retry only what retrying can fix: a refused connection, a rate limit, or
    # Brevo being briefly down. A 400 means the payload or the sender is wrong
    # and will be wrong every time, so it raises on the first attempt. Runs in a
    # background threadpool, so the sleeps cost nothing a caller waits on.
    resp = None
    for attempt in range(_BREVO_ATTEMPTS):
        last = attempt == _BREVO_ATTEMPTS - 1
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.post(_BREVO_ENDPOINT, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            if last:
                raise RuntimeError(f"Brevo unreachable after {_BREVO_ATTEMPTS} attempts: {exc}") from exc
            logger.warning("Brevo transport error (attempt %d): %s", attempt + 1, exc)
            time.sleep(_BREVO_BACKOFF_SECONDS * (attempt + 1))
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            if last:
                break
            logger.warning(
                "Brevo returned %s (attempt %d), retrying", resp.status_code, attempt + 1
            )
            time.sleep(_BREVO_BACKOFF_SECONDS * (attempt + 1))
            continue
        break
    if resp is None:  # unreachable: the loop either sets it or raises
        raise RuntimeError("Brevo send made no attempt")
    if resp.status_code not in (200, 201):
        logger.error("Brevo send failed: status=%s body=%s", resp.status_code, resp.text[:400])
        resp.raise_for_status()
    logger.info("Brevo: delivered to %s (subject=%r)", to_address, subject)


def _send_smtp(to_address: str, subject: str, html_body: str, text_body: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.email_from
    msg["To"] = to_address
    if text_body:
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(
            settings.smtp_host, settings.smtp_port, timeout=_SMTP_TIMEOUT_SECONDS
        ) as server:
            server.ehlo()
            if settings.smtp_port != 465:
                server.starttls()
            if settings.smtp_user and settings.smtp_password:
                server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(settings.email_from, to_address, msg.as_string())
    except (TimeoutError, OSError) as exc:
        # Reached the network and got nothing back. On Render this is almost
        # always the platform, not the credentials: free web services are
        # blocked from ports 25, 465 and 587, and port 25 is blocked on every
        # plan. Say so, because the raw error is a bare timeout that reads like
        # a wrong host.
        raise RuntimeError(
            f"SMTP connection to {settings.smtp_host}:{settings.smtp_port} failed "
            f"({type(exc).__name__}: {exc}). If this host blocks outbound SMTP "
            f"(Render free instances block 25, 465 and 587), set EMAIL_PROVIDER=brevo "
            f"and send over HTTPS instead."
        ) from exc
    logger.info("SMTP: delivered to %s (subject=%r)", to_address, subject)


def _send(to_address: str, subject: str, html_body: str, text_body: str = "") -> None:
    provider = settings.email_provider.lower()
    if provider == "brevo":
        _send_brevo(to_address, subject, html_body, text_body)
    elif provider == "smtp":
        _send_smtp(to_address, subject, html_body, text_body)
    else:
        raise ValueError(f"Unknown email provider: {provider!r}. Use 'brevo' or 'smtp'.")


def describe_config() -> dict:
    """What this deployment would do if asked to send, with no secret in it.

    Delivery failures are invisible from the outside: `send_sharing_invite`
    swallows them so an inviter's request still succeeds, which leaves the logs
    as the only evidence. This is the same picture without log access.
    """
    provider = settings.email_provider.lower()
    ready = bool(settings.brevo_api_key) if provider == "brevo" else settings.smtp_host != "localhost"
    return {
        "provider": provider,
        "configured": ready,
        "email_from": settings.email_from,
        "brevo_api_key_set": bool(settings.brevo_api_key),
        "smtp_host": settings.smtp_host,
        "smtp_port": settings.smtp_port,
        "smtp_credentials_set": bool(settings.smtp_user and settings.smtp_password),
    }


def send_test_email(to_address: str) -> None:
    """Send a throwaway message through the configured provider. Raises, rather
    than swallowing, so the caller can report why a send fails."""
    _send(
        to_address,
        "Orange Copy Paste: email test",
        _render_mail("Email works", _MAIL_TEST, "Sent by Orange Copy Paste as a delivery test."),
        "Email from this deployment works. Nothing else to do.\n",
    )


def send_sharing_invite(
    invitee_email: str,
    invitee_name: str,
    from_name: str,
    invite_code: str,
) -> None:
    """Best-effort delivery - logs and swallows failures so a background send
    never surfaces as a request error to the inviter.

    The link is https rather than `orange://`: mail clients strip or refuse to
    linkify a custom scheme, so the one thing the recipient is meant to click was
    often not clickable at all. The page it lands on hands the invite to the app.
    """
    subject = f"{from_name or 'Someone'} invited you to a space in Orange Copy Paste"
    join_url = web.join_url(invite_code)
    invitee = escape(invitee_name) or "there"
    inviter = escape(from_name) or "A user"
    # One display form everywhere: the app, the join page and this mail all show
    # the dashed code, so the recipient types back exactly what they read.
    display_code = spaces_service.format_invite_code(invite_code)
    code = escape(display_code)
    body = (
        _MAIL_INVITE.replace("__INVITEE__", invitee)
        .replace("__CODE__", code)
        .replace("__JOIN_URL__", escape(join_url, quote=True))
    )
    html = _render_mail(
        f"{inviter} invited you to a space",
        body,
        "Sent by Orange Copy Paste because someone entered your email address.",
    )
    text = (
        f"Hi {invitee_name or 'there'},\n\n"
        f"{from_name or 'A user'} invited you to a space in Orange Copy Paste.\n\n"
        f"Join: {join_url}\nInvite code: {display_code}\n\nThis invite expires in 24 hours.\n"
    )
    try:
        _send(invitee_email, subject, html, text)
    except Exception:
        logger.exception("Failed to send sharing invite email to %s", invitee_email)
