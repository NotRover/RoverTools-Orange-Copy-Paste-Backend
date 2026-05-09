"""Returns the active email provider singleton.

Switch providers by setting EMAIL_PROVIDER in .env:
  EMAIL_PROVIDER=brevo   (default)
  EMAIL_PROVIDER=smtp

Add a new provider by:
1. Implementing the EmailProvider protocol in a new module.
2. Registering it in _PROVIDERS below.
"""

from src.email.base import EmailProvider

_instance: EmailProvider | None = None


def get_provider() -> EmailProvider:
    global _instance
    if _instance is not None:
        return _instance

    from src.config import settings

    provider = settings.email_provider.lower()

    if provider == "brevo":
        from src.email.brevo import BrevoProvider
        _instance = BrevoProvider()
    elif provider == "smtp":
        from src.email.smtp import SmtpProvider
        _instance = SmtpProvider()
    else:
        raise ValueError(f"Unknown email provider: {provider!r}. Use 'brevo' or 'smtp'.")

    return _instance


def reset_provider() -> None:
    """Force re-initialisation (useful in tests)."""
    global _instance
    _instance = None
