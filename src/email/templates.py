"""Minimal email templates for auth flows.

All templates return (subject, html, text) tuples.
Replace with a proper template engine (Jinja2, MJML, etc.) when needed.
"""


def verification_email(display_name: str, verify_url: str) -> tuple[str, str, str]:
    subject = "Verify your Orange Clipboard email"
    html = f"""
<p>Hi {display_name or "there"},</p>
<p>Click the link below to verify your email address:</p>
<p><a href="{verify_url}">{verify_url}</a></p>
<p>This link expires in 24 hours.</p>
<p>If you didn't create an account, you can ignore this email.</p>
<p>— Orange Clipboard</p>
"""
    text = (
        f"Hi {display_name or 'there'},\n\n"
        f"Verify your email: {verify_url}\n\n"
        "This link expires in 24 hours.\n"
        "If you didn't create an account, ignore this email.\n"
    )
    return subject, html, text


def sharing_invite_email(
    invitee_name: str, from_name: str, invite_code: str, app_url: str = "orange://join"
) -> tuple[str, str, str]:
    subject = f"{from_name or 'Someone'} invited you to Orange Clipboard Live Share"
    join_url = f"{app_url}?code={invite_code}"
    html = f"""
<p>Hi {invitee_name or "there"},</p>
<p><strong>{from_name or "A user"}</strong> has invited you to join their Orange Clipboard Live Share session.</p>
<p>Click the link below to join (or enter the code in the app):</p>
<p><a href="{join_url}">{join_url}</a></p>
<p>Invite code: <strong>{invite_code}</strong></p>
<p>This invite expires in 24 hours.</p>
<p>— Orange Clipboard</p>
"""
    text = (
        f"Hi {invitee_name or 'there'},\n\n"
        f"{from_name or 'A user'} has invited you to join their Orange Clipboard Live Share session.\n\n"
        f"Join here: {join_url}\n"
        f"Invite code: {invite_code}\n\n"
        "This invite expires in 24 hours.\n"
    )
    return subject, html, text


def password_reset_email(display_name: str, reset_url: str) -> tuple[str, str, str]:
    subject = "Reset your Orange Clipboard password"
    html = f"""
<p>Hi {display_name or "there"},</p>
<p>We received a request to reset your password. Click the link below:</p>
<p><a href="{reset_url}">{reset_url}</a></p>
<p>This link expires in 1 hour. If you didn't request a reset, ignore this email.</p>
<p>— Orange Clipboard</p>
"""
    text = (
        f"Hi {display_name or 'there'},\n\n"
        f"Reset your password: {reset_url}\n\n"
        "This link expires in 1 hour.\n"
        "If you didn't request a reset, ignore this email.\n"
    )
    return subject, html, text
