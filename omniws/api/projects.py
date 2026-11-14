from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import projects
from ..config import Settings
from ..files import FileError, ProjectFiles

router = APIRouter(prefix="/api/projects")


class NewProject(BaseModel):
    name: str = Field(max_length=200)
    git_init: bool = True
    readme: bool = False
    gitignore: bool = False


class Clone(BaseModel):
    url: str = Field(max_length=500)
    name: str | None = Field(default=None, max_length=200)


def settings(request: Request) -> Settings:
    return request.app.state.settings


def failure(exc: projects.ProjectError) -> HTTPException:
    status = 409 if exc.code == "exists" else 400
    return HTTPException(status_code=status, detail={"code": exc.code, "message": exc.message})


@router.get("")
async def list_projects(request: Request):
    config = settings(request)
    return {"root": str(config.omni_projects_dir), "can_create": config.omni_allow_project_create,
            "can_clone": config.omni_allow_git_clone,
            "projects": await projects.list_projects(config.omni_projects_dir),
            "groups": await run_in_threadpool(projects.list_groups, config.omni_projects_dir),
            "max_depth": projects.MAX_DEPTH}


@router.post("", status_code=201)
async def create_project(body: NewProject, request: Request):
    config = settings(request)
    if not config.omni_allow_project_create:
        raise HTTPException(status_code=403, detail={"code": "not_allowed", "message": "OMNI_ALLOW_PROJECT_CREATE"})
    try:
        path = await run_in_threadpool(projects.create_project, config.omni_projects_dir, body.name, body.git_init,
                                       body.readme, body.gitignore)
    except projects.ProjectError as exc:
        raise failure(exc) from exc
    return {"name": projects.check_name(body.name)}


@router.post("/clone", status_code=201)
async def clone_project(body: Clone, request: Request):
    config = settings(request)
    if not config.omni_allow_git_clone:
        raise HTTPException(status_code=403, detail={"code": "not_allowed", "message": "OMNI_ALLOW_GIT_CLONE"})
    try:
        path = await run_in_threadpool(projects.clone_project, config.omni_projects_dir, body.url, body.name)
    except projects.ProjectError as exc:
        raise failure(exc) from exc
    return {"name": path.relative_to(config.omni_projects_dir).as_posix()}


async def browse(request: Request, name: str, method: str, *args):
    def read():
        try:
            return getattr(ProjectFiles(settings(request), name), method)(*args)
        except FileError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message) from exc
    return await run_in_threadpool(read)


@router.get("/{name:path}/files")
async def list_files(name: str, request: Request, path: str = Query(default="", max_length=4096),
                     offset: int = Query(default=0, ge=0), limit: int = Query(default=200, ge=1, le=500)):
    return await browse(request, name, "tree", path, offset, limit)


@router.get("/{name:path}/file")
async def read_file(name: str, request: Request, path: str = Query(max_length=4096)):
    return await browse(request, name, "file", path)


@router.get("/{name:path}/image")
async def read_image(name: str, request: Request, path: str = Query(max_length=4096)):
    data, media_type = await browse(request, name, "image", path)
    return Response(data, media_type=media_type, headers={"Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; sandbox"})


@router.get("/{name:path}/changes")
async def list_changes(name: str, request: Request):
    return await browse(request, name, "changes")


@router.get("/{name:path}/diff")
async def read_diff(name: str, request: Request, path: str = Query(max_length=4096),
                    stage: Literal["staged", "unstaged", "untracked"] = "unstaged"):
    return await browse(request, name, "diff", path, stage)
