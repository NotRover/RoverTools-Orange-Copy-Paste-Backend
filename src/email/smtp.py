"""Standard SMTP provider — fallback / self-hosted option.

Uses Python's stdlib smtplib.  Configure via SMTP_* env vars.
"""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from src.config import settings

logger = logging.getLogger(__name__)


class SmtpProvider:
    def send(
        self,
        to_address: str,
        subject: str,
        html_body: str,
        text_body: str = "",
    ) -> None:
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
