"""Serves /.well-known/* endpoints at the root of the application."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from src.auth.jwt import get_jwks

router = APIRouter(tags=["well-known"])


@router.get("/.well-known/jwks.json", include_in_schema=False)
async def jwks() -> JSONResponse:
    """RS256 public key in JWKS format for JWT verification by third-party consumers."""
    return JSONResponse(
        content=get_jwks(),
        headers={"Cache-Control": "public, max-age=3600"},
    )
