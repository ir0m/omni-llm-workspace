from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import compile as compiler
from .. import notes, projects
from ..config import Settings
from ..files import EDIT_LIMIT, FileError, ProjectFiles

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


class SaveFile(BaseModel):
    path: str = Field(max_length=4096)
    text: str = Field(max_length=EDIT_LIMIT)
    # The hash the editor received when it loaded the file; None only when creating
    base_hash: str | None = Field(default=None, max_length=64)
    create: bool = False


class CompileRequest(BaseModel):
    path: str = Field(max_length=4096)


class NewFolder(BaseModel):
    path: str = Field(max_length=4096)


class MoveFile(BaseModel):
    path: str = Field(max_length=4096)
    to: str = Field(max_length=4096)


async def browse(request: Request, name: str, method: str, *args):
    def read():
        try:
            return getattr(ProjectFiles(settings(request), name), method)(*args)
        except FileError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message) from exc
    return await run_in_threadpool(read)


@router.get("/{name:path}/files")
async def list_files(name: str, request: Request, path: str = Query(default="", max_length=4096),
                     offset: int = Query(default=0, ge=0), limit: int = Query(default=200, ge=1, le=500),
                     show_hidden: bool = True):
    return await browse(request, name, "tree", path, offset, limit, show_hidden)


@router.get("/{name:path}/file")
async def read_file(name: str, request: Request, path: str = Query(max_length=4096), full: bool = False):
    """`full` loads up to the editor's limit, so the whole file can be edited and saved back."""
    return await browse(request, name, "file", path, *([EDIT_LIMIT] if full else []))


async def edit(request: Request, name: str, method: str, *args):
    if not settings(request).omni_allow_file_edit:
        raise HTTPException(status_code=403, detail="ファイルの編集は無効です。OMNI_ALLOW_FILE_EDIT=true の設定が必要です")
    return await browse(request, name, method, *args)


@router.put("/{name:path}/file")
async def save_file(name: str, body: SaveFile, request: Request):
    return await edit(request, name, "write", body.path, body.text, body.base_hash, body.create)


@router.delete("/{name:path}/file")
async def delete_file(name: str, request: Request, path: str = Query(max_length=4096), recursive: bool = False,
                      expected: int | None = Query(default=None, ge=0)):
    """A folder with content needs `recursive` and the entry count from its summary (`expected`)."""
    return await edit(request, name, "delete", path, recursive, expected)


@router.get("/{name:path}/folder")
async def folder_summary(name: str, request: Request, path: str = Query(max_length=4096)):
    """What deleting a folder would remove: counts for the confirmation."""
    return await edit(request, name, "folder_summary", path)


@router.post("/{name:path}/folder", status_code=201)
async def make_folder(name: str, body: NewFolder, request: Request):
    return await edit(request, name, "make_folder", body.path)


@router.post("/{name:path}/move")
async def move_file(name: str, body: MoveFile, request: Request):
    return await edit(request, name, "move", body.path, body.to)


@router.get("/{name:path}/image")
async def read_image(name: str, request: Request, path: str = Query(max_length=4096)):
    data, media_type = await browse(request, name, "image", path)
    return Response(data, media_type=media_type, headers={"Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; sandbox"})


PDF_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
               "Content-Security-Policy": "default-src 'none'; sandbox"}


async def compiling(function, request: Request, name: str, path: str):
    def run():
        try:
            return function(settings(request), name, path)
        except compiler.CompileError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message) from exc
    return await run_in_threadpool(run)


@router.post("/{name:path}/compile")
async def compile_document(name: str, body: CompileRequest, request: Request):
    return await compiling(compiler.compile_document, request, name, body.path)


@router.get("/{name:path}/pdf")
async def read_pdf(name: str, request: Request, path: str = Query(max_length=4096)):
    """A PDF file of the project, or the last successful build of a .typ / .tex document."""
    if path.lower().endswith(".pdf"):
        return Response(await browse(request, name, "pdf", path), media_type="application/pdf", headers=PDF_HEADERS)
    return FileResponse(await compiling(compiler.built_pdf, request, name, path), media_type="application/pdf",
                        headers=PDF_HEADERS)


@router.get("/{name:path}/changes")
async def list_changes(name: str, request: Request):
    return await browse(request, name, "changes")


@router.get("/{name:path}/diff")
async def read_diff(name: str, request: Request, path: str = Query(max_length=4096),
                    stage: Literal["staged", "unstaged", "untracked"] = "unstaged"):
    return await browse(request, name, "diff", path, stage)


@router.get("/{name:path}/notes")
async def note_index(name: str, request: Request, q: str = Query(default="", max_length=300),
                     path: str = Query(default="", max_length=4096)):
    def run():
        try:
            return notes.index(settings(request), name, q, path)
        except FileError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message) from exc
    return await run_in_threadpool(run)
