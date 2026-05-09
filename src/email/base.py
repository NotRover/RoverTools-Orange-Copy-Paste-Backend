"""Abstract email provider interface.

Swap providers by changing EMAIL_PROVIDER in .env.
Any class implementing EmailProvider can be registered in factory.py.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class EmailProvider(Protocol):
    def send(
        self,
        to_address: str,
        subject: str,
        html_body: str,
        text_body: str = "",
    ) -> None:
        """Send a transactional email.  Raises on delivery failure."""
        ...
