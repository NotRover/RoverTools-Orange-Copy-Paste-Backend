# Supabase Auth email templates

Supabase sends every account email itself - confirm signup, password reset, email
change - from templates stored in its dashboard, not in this repo. These files are
paste-ready copies built from the same frame as the mail this service sends
(`src/web/templates/email_shell.html`), so a user gets one look across all of it.

Regenerate after any change to the shell, then paste again:

```
uv run python scripts/render_supabase_emails.py
```

## Where they go

Supabase dashboard -> Authentication -> Emails -> Templates. Paste the whole file
into the Message body field, and set the subject line beside it.

| File | Template | Subject |
|---|---|---|
| `confirm_signup.html` | Confirm signup | Confirm your Orange Copy Paste email |
| `reset_password.html` | Reset password | Set a new Orange Copy Paste password |
| `change_email.html` | Change email address | Confirm your new Orange Copy Paste email |
| `magic_link.html` | Magic Link | Your Orange Copy Paste sign-in link |

`{{ .ConfirmationURL }}` is Supabase's own placeholder and must survive editing -
it is the only link in the message.

## Two things worth knowing

Supabase's built-in mailer is rate limited to a couple of messages an hour and is
not meant for production. Configuring custom SMTP (Project settings -> Auth ->
SMTP) lifts that, and is what makes signup and reset mail dependable.

The reset link points at this service's `/reset` page rather than at a web form,
because a new password re-wraps the account's encryption key and only the app
holds that key.
