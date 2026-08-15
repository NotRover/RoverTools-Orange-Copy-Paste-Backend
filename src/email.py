"""Transactional email — sharing invites only.

Account emails (verification, password reset) are sent by Supabase Auth. The one
message the app sends itself is the Live Share invite. Provider is chosen by the
`EMAIL_PROVIDER` setting; both send paths are synchronous and are meant to run
via FastAPI `BackgroundTasks` (which executes them in a threadpool).
"""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

_BREVO_ENDPOINT = "https://api.brevo.com/v3/smtp/email"


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

    with httpx.Client(timeout=15) as client:
        resp = client.post(
            _BREVO_ENDPOINT,
            json=payload,
            headers={"api-key": settings.brevo_api_key, "Content-Type": "application/json"},
        )
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

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
        server.ehlo()
        if settings.smtp_port != 465:
            server.starttls()
        if settings.smtp_user and settings.smtp_password:
            server.login(settings.smtp_user, settings.smtp_password)
        server.sendmail(settings.email_from, to_address, msg.as_string())
    logger.info("SMTP: delivered to %s (subject=%r)", to_address, subject)


def _send(to_address: str, subject: str, html_body: str, text_body: str = "") -> None:
    provider = settings.email_provider.lower()
    if provider == "brevo":
        _send_brevo(to_address, subject, html_body, text_body)
    elif provider == "smtp":
        _send_smtp(to_address, subject, html_body, text_body)
    else:
        raise ValueError(f"Unknown email provider: {provider!r}. Use 'brevo' or 'smtp'.")


def send_sharing_invite(
    invitee_email: str,
    invitee_name: str,
    from_name: str,
    invite_code: str,
    app_url: str = "orange://join",
) -> None:
    """Best-effort delivery — logs and swallows failures so a background send
    never surfaces as a request error to the inviter."""
    subject = f"{from_name or 'Someone'} invited you to Orange Clipboard Live Share"
    join_url = f"{app_url}?code={invite_code}"
    html = f"""
<p>Hi {invitee_name or "there"},</p>
<p><strong>{from_name or "A user"}</strong> invited you to join their Orange Clipboard Live Share session.</p>
<p>Open the app and enter this code, or use the link below:</p>
<p><a href="{join_url}">{join_url}</a></p>
<p>Invite code: <strong>{invite_code}</strong></p>
<p>This invite expires in 24 hours.</p>
<p>Orange Clipboard</p>
"""
    text = (
        f"Hi {invitee_name or 'there'},\n\n"
        f"{from_name or 'A user'} invited you to their Orange Clipboard Live Share session.\n\n"
        f"Join: {join_url}\nInvite code: {invite_code}\n\nThis invite expires in 24 hours.\n"
    )
    try:
        _send(invitee_email, subject, html, text)
    except Exception:
        logger.exception("Failed to send sharing invite email to %s", invitee_email)
