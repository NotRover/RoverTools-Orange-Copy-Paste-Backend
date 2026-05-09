"""Brevo (formerly Sendinblue) transactional email provider.

Uses Brevo's REST API directly via httpx — no SDK dependency required.
API docs: https://developers.brevo.com/reference/sendtransacemail
"""

import logging

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

_BREVO_SMTP_ENDPOINT = "https://api.brevo.com/v3/smtp/email"


class BrevoProvider:
    def send(
        self,
        to_address: str,
        subject: str,
        html_body: str,
        text_body: str = "",
    ) -> None:
        if not settings.brevo_api_key:
            raise RuntimeError("BREVO_API_KEY is not configured")

        sender_parts = settings.email_from.split("<")
        sender_name = sender_parts[0].strip() if len(sender_parts) > 1 else "Orange Clipboard"
        sender_email = sender_parts[-1].rstrip(">").strip()

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
                _BREVO_SMTP_ENDPOINT,
                json=payload,
                headers={
                    "api-key": settings.brevo_api_key,
                    "Content-Type": "application/json",
                },
            )

        if resp.status_code not in (200, 201):
            logger.error(
                "Brevo send failed: status=%s body=%s",
                resp.status_code,
                resp.text[:400],
            )
            resp.raise_for_status()

        logger.info("Brevo: delivered to %s (subject=%r)", to_address, subject)
