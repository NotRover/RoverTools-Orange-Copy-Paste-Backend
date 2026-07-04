"""Thin client for the Supabase Auth (GoTrue) Admin API.

Used by the admin endpoints to read account state (email, verification, ban) and
to ban/unban or delete the Supabase-managed user. Requires SUPABASE_URL and the
service-role key; if either is missing, callers get 503. The service-role key is
server-only and must never reach a client.
"""

import httpx
from fastapi import HTTPException, status

from src.config import settings

# Effectively-permanent ban (~100 years); GoTrue expects a Go duration string.
_BAN_FOREVER = "876000h"


def _require_config() -> tuple[str, dict[str, str]]:
    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Supabase admin is not configured (SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY)",
        )
    base = settings.supabase_url.rstrip("/")
    headers = {
        "apikey": settings.supabase_service_role_key,
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
        "Content-Type": "application/json",
    }
    return base, headers


async def get_user(user_id: str) -> dict | None:
    base, headers = _require_config()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{base}/auth/v1/admin/users/{user_id}", headers=headers)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


async def set_banned(user_id: str, banned: bool) -> None:
    base, headers = _require_config()
    body = {"ban_duration": _BAN_FOREVER if banned else "none"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.put(f"{base}/auth/v1/admin/users/{user_id}", headers=headers, json=body)
    resp.raise_for_status()


async def delete_user(user_id: str) -> None:
    base, headers = _require_config()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.delete(f"{base}/auth/v1/admin/users/{user_id}", headers=headers)
    if resp.status_code not in (200, 204, 404):
        resp.raise_for_status()
