from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/executors")


@router.get("")
async def list_executors(request: Request, refresh: bool = False):
    """What can run a turn: providers (API, local) now, agents (subscription CLIs, OpenCode) as they are added.
    Each has the modes it supports and its models."""
    return await request.app.state.registry.overview(refresh)
