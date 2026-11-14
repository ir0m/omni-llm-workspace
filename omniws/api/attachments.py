"""Uploading files to attach to a message, and showing them in the conversation."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from .. import attachments as files
from ..models import Message

router = APIRouter(prefix="/api/attachments")
CHUNK = 1024 * 1024


def failure(exc: files.AttachmentError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.message)


@router.post("", status_code=201)
async def upload(request: Request, file: UploadFile):
    # Read in pieces so an oversized upload is refused without holding all of it
    data = bytearray()
    while chunk := await file.read(CHUNK):
        data += chunk
        if len(data) > files.FILE_LIMIT:
            raise HTTPException(status_code=413, detail="添付できるファイルは20 MiBまでです")
    try:
        return await run_in_threadpool(files.save, request.app.state.settings.omni_data_dir, bytes(data), file.filename)
    except files.AttachmentError as exc:
        raise failure(exc) from exc


@router.get("/{attachment_id}")
async def download(attachment_id: str, request: Request, download: bool = False):
    """Images are shown in the page; every other kind is only ever downloaded, never rendered."""
    data_dir = request.app.state.settings.omni_data_dir
    try:
        meta = await run_in_threadpool(files.meta, data_dir, attachment_id)
    except files.AttachmentError as exc:
        raise failure(exc) from exc
    inline = meta["kind"] == "image" and not download
    disposition = f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(meta['name'])}"
    return FileResponse(files.path(data_dir, attachment_id),
                        media_type=meta["media_type"] if inline else "application/octet-stream",
                        headers={"Content-Disposition": disposition, "Cache-Control": "private, max-age=86400",
                                 "X-Content-Type-Options": "nosniff",
                                 "Content-Security-Policy": "default-src 'none'; sandbox"})


def collect(state) -> int:
    """Removes files no message refers to any more (deleted conversations, uploads never sent)."""
    with state.sessions() as session:
        contents = session.scalars(select(Message.content).where(Message.role == "user")).all()
    return files.collect(state.settings.omni_data_dir, files.referenced(contents))
