from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/sessions")


@router.post("/discover")
async def discover(request: Request):
    return await request.app.state.external_sessions.discover()
