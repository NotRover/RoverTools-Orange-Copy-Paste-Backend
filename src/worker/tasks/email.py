import logging

from src.email.factory import get_provider
from src.email.templates import password_reset_email, sharing_invite_email, verification_email
from src.worker.app import app

logger = logging.getLogger(__name__)


@app.task(
    name="src.worker.tasks.email.send_verification_email",
    autoretry_for=(Exception,),
    max_retries=3,
    retry_backoff=True,
)
def send_verification_email(user_email: str, display_name: str, verify_url: str) -> None:
    subject, html, text = verification_email(display_name, verify_url)
    get_provider().send(user_email, subject, html, text)
    logger.info("Verification email sent to %s", user_email)


@app.task(
    name="src.worker.tasks.email.send_sharing_invite_email",
    autoretry_for=(Exception,),
    max_retries=3,
    retry_backoff=True,
)
def send_sharing_invite_email(
    invitee_email: str, invitee_name: str, from_name: str, invite_code: str
) -> None:
    subject, html, text = sharing_invite_email(invitee_name, from_name, invite_code)
    get_provider().send(invitee_email, subject, html, text)
    logger.info("Sharing invite email sent to %s", invitee_email)


@app.task(
    name="src.worker.tasks.email.send_password_reset_email",
    autoretry_for=(Exception,),
    max_retries=3,
    retry_backoff=True,
)
def send_password_reset_email(user_email: str, display_name: str, reset_url: str) -> None:
    subject, html, text = password_reset_email(display_name, reset_url)
    get_provider().send(user_email, subject, html, text)
    logger.info("Password-reset email sent to %s", user_email)
