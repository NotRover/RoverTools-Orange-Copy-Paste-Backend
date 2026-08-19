"""Generate the Supabase Auth email templates from this repo's mail shell.

Supabase sends every account email - confirm signup, password reset, email change
- from templates stored in its own dashboard, so they cannot import anything from
here. Without a generator the two sets drift, and a user gets one look when they
sign up and another when they are invited.

So: render them from `src/web/templates/email_shell.html`, commit the output, and
paste it into Authentication -> Email Templates. Re-run after any change to the
shell.

    uv run python scripts/render_supabase_emails.py

Supabase substitutes Go template variables (`{{ .ConfirmationURL }}`) in what it
sends, so those stay verbatim in the output.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHELL = (ROOT / "src/web/templates/email_shell.html").read_text(encoding="utf-8")
OUT = ROOT / "docs/supabase-email"

_FOOTER = "Sent by Orange Copy Paste. You can ignore this message if it was not meant for you."


def _button(label: str) -> str:
    """A padded table cell, not a styled anchor: Outlook ignores padding on an
    inline element and the click target collapses to the width of the text."""
    return (
        '            <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 20px;">\n'
        "              <tr>\n"
        '                <td align="center" bgcolor="#ff5535" style="border-radius:8px;">\n'
        '                  <a href="{{ .ConfirmationURL }}" style="display:inline-block;padding:11px 20px;'
        "font-family:'Segoe UI',-apple-system,BlinkMacSystemFont,Helvetica,Arial,sans-serif;font-size:13px;"
        'font-weight:600;color:#ffffff;text-decoration:none;">\n'
        f"                    {label}\n"
        "                  </a>\n"
        "                </td>\n"
        "              </tr>\n"
        "            </table>\n"
    )


def _para(text: str, muted: str = "#a0a0a0", size: str = "13px") -> str:
    return (
        f'            <p style="margin:0 0 18px;font-size:{size};line-height:1.5;color:{muted};">\n'
        f"              {text}\n"
        "            </p>\n"
    )


def _fine(text: str) -> str:
    return (
        '            <p style="margin:0;padding-top:18px;border-top:1px solid #272727;'
        'font-size:11px;line-height:1.5;color:#606060;">\n'
        f"              {text}\n"
        "            </p>\n"
    )


TEMPLATES = {
    "confirm_signup": (
        "Confirm your email",
        _para(
            "You signed up for Orange Copy Paste. Confirm this address and your clipboard "
            "and notes start syncing between your devices, end to end encrypted."
        )
        + _button("Confirm email")
        + _fine(
            "The link works once and expires in 24 hours. If you did not sign up, ignore "
            "this message and no account is created."
        ),
    ),
    "reset_password": (
        "Set a new password",
        _para(
            "Open this link on a device with Orange Copy Paste installed. The app sets the "
            "new password, because your password also unwraps your encryption key and the "
            "server never holds that key."
        )
        + _button("Set a new password")
        + _fine(
            "The link works once and expires in an hour. If you did not ask for it, ignore "
            "this message - your current password keeps working."
        ),
    ),
    "change_email": (
        "Confirm your new email",
        _para(
            "Confirm this address to finish moving your Orange Copy Paste account to it. "
            "Your synced items and spaces are unaffected."
        )
        + _button("Confirm new email")
        + _fine("If you did not ask to change your email, ignore this message."),
    ),
    "magic_link": (
        "Your sign-in link",
        _para("Open this link to sign in to Orange Copy Paste. No password needed.")
        + _button("Sign in")
        + _fine("The link works once and expires in an hour."),
    ),
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, (heading, body) in TEMPLATES.items():
        page = (
            SHELL.replace("__HEADING__", heading)
            .replace("__BODY__", body.rstrip("\n"))
            .replace("__FOOTER__", _FOOTER)
        )
        (OUT / f"{name}.html").write_text(page, encoding="utf-8")
        print(f"wrote docs/supabase-email/{name}.html")


if __name__ == "__main__":
    main()
