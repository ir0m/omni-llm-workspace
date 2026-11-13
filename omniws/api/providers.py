from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/providers")


@router.get("")
async def list_providers(request: Request, refresh: bool = False):
    """Every known provider with whether it is configured and, if so, its models."""
    return await request.app.state.registry.overview(refresh)
