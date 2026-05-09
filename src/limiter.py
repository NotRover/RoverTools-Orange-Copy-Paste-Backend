"""Global rate limiter instance (slowapi).

Import `limiter` in routers that need rate-limited endpoints.
The limiter is attached to app.state in main.py so slowapi can find it.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, default_limits=[])
