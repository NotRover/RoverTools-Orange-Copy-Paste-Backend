from src.worker.app import app


@app.task(name="src.worker.tasks.email.send_verification_email")
def send_verification_email(user_email: str, token: str) -> None:
    # TODO: integrate SMTP (Phase 7)
    pass


@app.task(name="src.worker.tasks.email.send_password_reset_email")
def send_password_reset_email(user_email: str, token: str) -> None:
    # TODO: integrate SMTP (Phase 7)
    pass
