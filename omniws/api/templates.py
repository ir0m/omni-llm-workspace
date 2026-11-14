from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import templates
from ..files import FileError

router = APIRouter(prefix="/api/templates")


class NewTemplate(BaseModel):
    name: str = Field(max_length=100)
    # "template:<name>" or "project:<name>" to start from a copy
    source: str | None = Field(default=None, max_length=300)


class ApplyItem(BaseModel):
    path: str = Field(max_length=4096)
    # The hash the plan showed for a file that exists; None for a new file
    base_hash: str | None = Field(default=None, max_length=64)


class Apply(BaseModel):
    project: str = Field(max_length=200)
    files: list[ApplyItem] = Field(max_length=templates.MAX_FILES)


async def call(function, *args):
    def run():
        try:
            return function(*args)
        except (templates.TemplateError, FileError) as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message) from exc
    return await run_in_threadpool(run)


def editing(request: Request):
    settings = request.app.state.settings
    if not settings.omni_allow_file_edit:
        raise HTTPException(status_code=403, detail="ファイルの編集は無効です。OMNI_ALLOW_FILE_EDIT=true の設定が必要です")
    return settings


@router.get("")
async def list_templates(request: Request):
    return {"group": templates.TEMPLATE_GROUP,
            "templates": await call(templates.list_templates, request.app.state.settings)}


@router.post("", status_code=201)
async def create_template(body: NewTemplate, request: Request):
    return await call(templates.create, editing(request), body.name, body.source)


@router.get("/{name}/plan")
async def plan_template(name: str, request: Request, project: str = Query(max_length=200)):
    return await call(templates.plan, request.app.state.settings, name, project)


@router.post("/{name}/apply")
async def apply_template(name: str, body: Apply, request: Request):
    return await call(templates.apply, editing(request), name, body.project,
                      [item.model_dump() for item in body.files])
