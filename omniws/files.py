"""Read-only project browsing and Git diffs. Never follow links below a registered project."""

from __future__ import annotations

import codecs
import difflib
import errno
import os
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .config import Settings
from .projects import ProjectError, is_project, project_path

TEXT_LIMIT = 256 * 1024
IMAGE_LIMIT = 8 * 1024 * 1024
GIT_LIMIT = 2 * 1024 * 1024
PATCH_LIMIT = 512 * 1024
PATCH_LINES = 5000
PRIVATE_DIRS = {".git", "data", ".ssh", ".aws", ".gnupg", ".codex", ".claude"}
PRIVATE_FILES = {"credentials", "credentials.json", "auth.json", "token", "token.json", "tokens.json",
                 "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", ".npmrc", ".netrc", ".pypirc"}


class FileError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message
        super().__init__(message)


def visible(parts: tuple[str, ...]) -> bool:
    for part in parts:
        lower = part.lower()
        if lower in PRIVATE_DIRS or lower in PRIVATE_FILES:
            return False
        if lower.startswith(".env") and lower not in (".env.example", ".env.sample", ".env.template"):
            return False
        if lower.endswith((".pem", ".key", ".p12", ".pfx", ".keystore")):
            return False
    return True


def parts_of(path: str) -> tuple[str, ...]:
    if "\0" in path or "\\" in path or path.startswith("/"):
        raise FileError(400, "プロジェクト内の相対パスを指定してください")
    parts = PurePosixPath(path).parts
    if ".." in parts or not visible(parts):
        raise FileError(403, "このパスは表示できません")
    return parts


class ProjectFiles:
    def __init__(self, settings: Settings, name: str):
        try:
            registered = project_path(settings.omni_projects_dir, name)
        except ProjectError as exc:
            raise FileError(400, "プロジェクト名が正しくありません") from exc
        if not registered.is_dir() or not is_project(settings.omni_projects_dir, name):
            raise FileError(404, "プロジェクトが見つかりません")
        # A registered project itself may be a symlink, as in the self-hosted installation.
        self.root = registered.resolve()
        self.private_data = settings.omni_data_dir.resolve()
        if self.root.is_relative_to(self.private_data):
            raise FileError(403, "アプリのデータフォルダは表示できません")

    def allowed(self, parts: tuple[str, ...]) -> bool:
        return visible(parts) and not self.root.joinpath(*parts).is_relative_to(self.private_data)

    @contextmanager
    def opened(self, path: str, *, directory: bool = False):
        parts = parts_of(path)
        if not self.allowed(parts):
            raise FileError(403, "このパスは表示できません")
        descriptor = None
        try:
            descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            for index, part in enumerate(parts):
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if index < len(parts) - 1 or directory:
                    flags |= os.O_DIRECTORY
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            info = os.fstat(descriptor)
            if directory and not stat.S_ISDIR(info.st_mode):
                raise FileError(400, "フォルダを指定してください")
            if not directory and not stat.S_ISREG(info.st_mode):
                raise FileError(415, "通常のファイルだけ表示できます")
            yield descriptor, info
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                raise FileError(404, "ファイルが見つかりません") from exc
            raise FileError(403, "このパスは読み込めません（リンクやアクセス権を確認してください）") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def tree(self, path: str, offset: int, limit: int) -> dict:
        parts = parts_of(path)
        with self.opened(path, directory=True) as (descriptor, _):
            entries = []
            with os.scandir(descriptor) as scan:
                for entry in scan:
                    child = (*parts, entry.name)
                    if not self.allowed(child):
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue  # files can disappear while the agent works
                    kind = "directory" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "other"
                    if stat.S_ISLNK(info.st_mode):
                        kind = "link"
                    entries.append({"name": entry.name, "path": "/".join(child), "kind": kind, "size": info.st_size})
            entries.sort(key=lambda item: (item["kind"] != "directory", item["name"].casefold(), item["name"]))
            end = offset + limit
            return {"path": "/".join(parts), "entries": entries[offset:end],
                    "next_offset": end if end < len(entries) else None}

    @staticmethod
    def image_type(data: bytes) -> str | None:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return "image/webp"
        return None

    def file(self, path: str) -> dict:
        with self.opened(path) as (descriptor, info):
            with os.fdopen(os.dup(descriptor), "rb") as stream:
                data = stream.read(TEXT_LIMIT + 1)
        image_type = self.image_type(data)
        base = {"path": path, "size": info.st_size}
        if image_type:
            return {**base, "kind": "image", "media_type": image_type, "too_large": info.st_size > IMAGE_LIMIT}
        truncated = len(data) > TEXT_LIMIT
        try:
            # Ignore an incomplete final UTF-8 character only when the preview is truncated.
            text = codecs.getincrementaldecoder("utf-8")("strict").decode(data[:TEXT_LIMIT], final=not truncated)
        except UnicodeDecodeError:
            return {**base, "kind": "binary"}
        if b"\0" in data:
            return {**base, "kind": "binary"}
        return {**base, "kind": "text", "text": text, "truncated": truncated}

    def image(self, path: str) -> tuple[bytes, str]:
        with self.opened(path) as (descriptor, info):
            if info.st_size > IMAGE_LIMIT:
                raise FileError(413, "画像は8 MiBまで表示できます")
            with os.fdopen(os.dup(descriptor), "rb") as stream:
                data = stream.read(IMAGE_LIMIT + 1)
        media_type = self.image_type(data)
        if len(data) > IMAGE_LIMIT:
            raise FileError(413, "画像は8 MiBまで表示できます")
        if not media_type:
            raise FileError(415, "この画像形式は表示できません")
        return data, media_type

    def git(self, *args: str) -> bytes:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
        command = ["git", "--no-pager", "--no-optional-locks", "--literal-pathspecs", "-C", str(self.root),
                   "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}", *args]
        try:
            # Bound memory use even for a repository with a large diff or many untracked files.
            with tempfile.TemporaryFile() as output:
                result = subprocess.run(command, stdout=output, stderr=subprocess.DEVNULL, env=env, timeout=8)
                if result.returncode:
                    raise FileError(409, "Gitの状態を取得できませんでした。更新して再度お試しください")
                output.seek(0)
                data = output.read(GIT_LIMIT + 1)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FileError(503, "Gitの読み込みが時間内に完了しませんでした") from exc
        if len(data) > GIT_LIMIT:
            raise FileError(413, "変更が大きすぎるため表示できません")
        return data

    def changes(self) -> dict:
        # Never accidentally expose the parent repository when the project is a subdirectory.
        if not (self.root / ".git").exists():
            return {"is_git": False, "staged": [], "unstaged": [], "untracked": []}
        if Path(os.fsdecode(self.git("rev-parse", "--show-toplevel")).rstrip("\n")).resolve() != self.root:
            raise FileError(403, "プロジェクトの外側のGitリポジトリは表示できません")
        fields = self.git("status", "--porcelain=v1", "-z", "--untracked-files=all").split(b"\0")
        result = {"is_git": True, "staged": [], "unstaged": [], "untracked": []}
        index = 0
        while index < len(fields):
            raw = fields[index]
            index += 1
            if not raw:
                continue
            status_code, path = raw[:2].decode("ascii"), os.fsdecode(raw[3:])
            old_path = None
            if "R" in status_code or "C" in status_code:
                old_path = os.fsdecode(fields[index])
                index += 1
            try:
                if not self.allowed(parts_of(path)) or (old_path and not self.allowed(parts_of(old_path))):
                    continue
            except FileError:
                continue
            item = {"path": path, "old_path": old_path}
            if status_code == "??":
                result["untracked"].append({**item, "status": "?"})
            else:
                if status_code[0] != " ":
                    result["staged"].append({**item, "status": status_code[0]})
                if status_code[1] != " ":
                    result["unstaged"].append({**item, "status": status_code[1]})
        return result

    def diff(self, path: str, stage: str) -> dict:
        parts = parts_of(path)
        if not self.allowed(parts):
            raise FileError(403, "このパスは表示できません")
        changed = self.changes()
        item = next((item for item in changed.get(stage, []) if item["path"] == path), None)
        if item is None:
            raise FileError(404, "この変更は見つかりません。差分一覧を更新してください")
        for target in (path, item["old_path"]):
            if target:
                try:
                    with self.opened(target):
                        pass
                except FileError as exc:
                    if exc.status != 404:
                        raise
        if stage == "untracked":
            file = self.file(path)
            if file["kind"] != "text":
                return {"path": path, "patch": "バイナリファイル（未追跡）", "truncated": False}
            patch = "".join(difflib.unified_diff([], file["text"].splitlines(keepends=True),
                                               fromfile="/dev/null", tofile=f"b/{path}"))
            truncated = file["truncated"]
        else:
            args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames", "--submodule=short"]
            if stage == "staged":
                args.append("--cached")
            paths = [item["old_path"], path] if item["old_path"] else [path]
            patch = self.git(*args, "--", *paths).decode("utf-8", errors="replace")
            truncated = False
        lines = patch[:PATCH_LIMIT].splitlines(keepends=True)
        return {"path": path, "patch": "".join(lines[:PATCH_LINES]),
                "truncated": truncated or len(patch) > PATCH_LIMIT or len(lines) > PATCH_LINES}
