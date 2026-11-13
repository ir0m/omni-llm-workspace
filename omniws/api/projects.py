from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import projects
from ..config import Settings

router = APIRouter(prefix="/api/projects")


class NewProject(BaseModel):
    name: str = Field(max_length=100)
    git_init: bool = True
    readme: bool = False
    gitignore: bool = False


class Clone(BaseModel):
    url: str = Field(max_length=500)
    name: str | None = Field(default=None, max_length=100)


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
            "projects": await projects.list_projects(config.omni_projects_dir)}


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
    return {"name": path.name}


@router.post("/clone", status_code=201)
async def clone_project(body: Clone, request: Request):
    config = settings(request)
    if not config.omni_allow_git_clone:
        raise HTTPException(status_code=403, detail={"code": "not_allowed", "message": "OMNI_ALLOW_GIT_CLONE"})
    try:
        path = await run_in_threadpool(projects.clone_project, config.omni_projects_dir, body.url, body.name)
    except projects.ProjectError as exc:
        raise failure(exc) from exc
    return {"name": path.name}
