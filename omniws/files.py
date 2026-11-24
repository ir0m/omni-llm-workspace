"""Project browsing, Git diffs and human edits from the screen. Never follow links below a registered project."""

from __future__ import annotations

import codecs
import difflib
import errno
import hashlib
import os
import secrets
import shutil
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .config import Settings
from .projects import ProjectError, is_project, project_path

TEXT_LIMIT = 256 * 1024
EDIT_LIMIT = 2 * 1024 * 1024  # the largest text file the editor loads and saves
IMAGE_LIMIT = 8 * 1024 * 1024
PDF_LIMIT = 64 * 1024 * 1024
GIT_LIMIT = 2 * 1024 * 1024
PATCH_LIMIT = 512 * 1024
PATCH_LINES = 5000
FOLDER_DELETE_LIMIT = 50_000  # entries; larger trees are left to the terminal or an agent
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


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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

    def tree(self, path: str, offset: int, limit: int, show_hidden: bool = True) -> dict:
        parts = parts_of(path)
        with self.opened(path, directory=True) as (descriptor, _):
            entries = []
            with os.scandir(descriptor) as scan:
                for entry in scan:
                    if not show_hidden and entry.name.startswith("."):
                        continue
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

    def file(self, path: str, limit: int = TEXT_LIMIT) -> dict:
        with self.opened(path) as (descriptor, info):
            with os.fdopen(os.dup(descriptor), "rb") as stream:
                data = stream.read(limit + 1)
        image_type = self.image_type(data)
        base = {"path": path, "size": info.st_size}
        if image_type:
            return {**base, "kind": "image", "media_type": image_type, "too_large": info.st_size > IMAGE_LIMIT}
        if data.startswith(b"%PDF-"):
            return {**base, "kind": "pdf", "too_large": info.st_size > PDF_LIMIT}
        truncated = len(data) > limit
        try:
            # Ignore an incomplete final UTF-8 character only when the preview is truncated.
            text = codecs.getincrementaldecoder("utf-8")("strict").decode(data[:limit], final=not truncated)
        except UnicodeDecodeError:
            return {**base, "kind": "binary"}
        if b"\0" in data:
            return {**base, "kind": "binary"}
        # The hash identifies the whole content, so a truncated preview cannot be saved back over the file.
        return {**base, "kind": "text", "text": text, "truncated": truncated,
                "hash": None if truncated else content_hash(data)}

    @contextmanager
    def parent(self, path: str):
        """The open folder that holds `path`, and the entry's name. Refuses the project root itself."""
        parts = parts_of(path)
        if not parts:
            raise FileError(400, "ファイルまたはフォルダのパスを指定してください")
        if len(parts[-1].encode()) > 255:
            raise FileError(400, "名前が長すぎます")
        if not self.allowed(parts):
            raise FileError(403, "このパスは編集できません")
        with self.opened("/".join(parts[:-1]), directory=True) as (descriptor, _):
            yield descriptor, parts[-1]

    @staticmethod
    def entry(descriptor: int, name: str) -> os.stat_result | None:
        try:
            return os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def write(self, path: str, text: str, base_hash: str | None, create: bool) -> dict:
        """Save a text file. An existing file is replaced only while it still has the content the editor loaded."""
        data = text.encode("utf-8")
        if len(data) > EDIT_LIMIT:
            raise FileError(413, "2 MiBを超えるファイルは保存できません")
        with self.parent(path) as (descriptor, name):
            info = self.entry(descriptor, name)
            if info is not None and not stat.S_ISREG(info.st_mode):
                raise FileError(409 if stat.S_ISDIR(info.st_mode) else 403, "通常のファイルだけ保存できます")
            if create and info is not None:
                raise FileError(409, "同じ名前のファイルが既にあります")
            if not create:
                if info is None:
                    raise FileError(404, "ファイルが見つかりません。削除または改名された可能性があります")
                if info.st_size > EDIT_LIMIT:
                    raise FileError(409, "ファイルが変更されています。読み込み直してください")
                current = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
                with os.fdopen(current, "rb") as stream:
                    if content_hash(stream.read(EDIT_LIMIT + 1)) != base_hash:
                        raise FileError(409, "ファイルが変更されています。読み込み直してください")
            # Write beside the target and rename over it, so a failed save never leaves half a file.
            temporary = f".{name[:200]}.{secrets.token_hex(6)}.tmp"
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666, dir_fd=descriptor)
            try:
                with os.fdopen(handle, "wb") as stream:
                    if info is not None:
                        os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))  # new files follow the umask
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                if create:
                    # A hard link fails when the name was taken meanwhile; a rename would silently replace it.
                    os.link(temporary, name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
                else:
                    os.replace(temporary, name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
                    temporary = None
            except FileExistsError as exc:
                raise FileError(409, "同じ名前のファイルが既にあります") from exc
            finally:
                if temporary is not None:
                    try:
                        os.unlink(temporary, dir_fd=descriptor)
                    except OSError:
                        pass
        return {"path": path, "size": len(data), "hash": content_hash(data)}

    def make_folder(self, path: str) -> dict:
        with self.parent(path) as (descriptor, name):
            try:
                os.mkdir(name, 0o755, dir_fd=descriptor)
            except FileExistsError as exc:
                raise FileError(409, "同じ名前のファイルまたはフォルダが既にあります") from exc
        return {"path": path}

    def move(self, path: str, to: str) -> dict:
        """Rename or move a file or folder inside the project. Never replaces an existing entry."""
        target_parts = parts_of(to)
        if target_parts[:len(parts_of(path))] == parts_of(path):
            raise FileError(400, "フォルダを自身の中へは移動できません")
        with self.parent(path) as (source, name), self.parent(to) as (target, new_name):
            info = self.entry(source, name)
            if info is None:
                raise FileError(404, "ファイルが見つかりません")
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise FileError(403, "リンク・特殊ファイルは移動できません")
            if self.entry(target, new_name) is not None:
                raise FileError(409, "同じ名前のファイルまたはフォルダが既にあります")
            os.rename(name, new_name, src_dir_fd=source, dst_dir_fd=target)
        return {"path": to}

    def folder_contents(self, descriptor: int, name: str, parts: tuple[str, ...]) -> dict:
        """Count what deleting a folder removes. Refuses trees holding what Docs never shows (secrets, .git,
        the app's data), other file systems (mount points) and special files, so a deletion never removes
        something the user could not see in the list."""
        files = folders = size = 0
        root = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
        device = os.fstat(root).st_dev
        stack: list[tuple[int, tuple[str, ...]]] = [(root, parts)]
        try:
            while stack:
                folder, where = stack.pop()
                try:
                    with os.scandir(folder) as scan:
                        entries = list(scan)
                    for entry in entries:
                        child = (*where, entry.name)
                        if not self.allowed(child):
                            raise FileError(409, f"{'/'.join(child)} は Docs に表示されない項目（秘密情報・.git・アプリのデータなど）のため、"
                                                 "このフォルダは削除できません。端末やエージェントで削除してください")
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode):
                            if info.st_dev != device:
                                raise FileError(409, f"{'/'.join(child)} は別のファイルシステムのため、このフォルダは削除できません")
                            folders += 1
                            stack.append((os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder), child))
                        elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                            files += 1
                            size += info.st_size if stat.S_ISREG(info.st_mode) else 0
                        else:
                            raise FileError(409, f"{'/'.join(child)} は特殊ファイルのため、このフォルダは削除できません")
                        if files + folders > FOLDER_DELETE_LIMIT:
                            raise FileError(413, "項目が多すぎるため削除できません。端末やエージェントで削除してください")
                finally:
                    os.close(folder)
        finally:
            for folder, _ in stack:
                os.close(folder)
        return {"files": files, "folders": folders, "size": size}

    def folder_summary(self, path: str) -> dict:
        """What deleting the folder at `path` removes, for the confirmation."""
        parts = parts_of(path)
        with self.parent(path) as (descriptor, name):
            info = self.entry(descriptor, name)
            if info is None:
                raise FileError(404, "フォルダが見つかりません")
            if not stat.S_ISDIR(info.st_mode):
                raise FileError(400, "フォルダを指定してください")
            try:
                return {"path": path, **self.folder_contents(descriptor, name, parts)}
            except FileNotFoundError as exc:
                raise FileError(409, "フォルダの中身が変わりました。もう一度お試しください") from exc

    def delete(self, path: str, recursive: bool = False, expected: int | None = None) -> dict:
        """Delete a file, a link, or a folder. A folder with content needs `recursive` and the number of entries
        the user confirmed (`expected`), so nothing added after the confirmation is removed unseen."""
        parts = parts_of(path)
        with self.parent(path) as (descriptor, name):
            info = self.entry(descriptor, name)
            if info is None:
                raise FileError(404, "ファイルが見つかりません")
            if stat.S_ISDIR(info.st_mode):
                try:
                    os.rmdir(name, dir_fd=descriptor)
                except OSError as exc:
                    if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                        raise
                    if not recursive:
                        raise FileError(409, "空でないフォルダです。中身ごと削除するには確認が必要です") from exc
                    try:
                        contents = self.folder_contents(descriptor, name, parts)
                    except FileNotFoundError as missing:
                        raise FileError(409, "フォルダの中身が変わりました。もう一度お試しください") from missing
                    if expected is None or contents["files"] + contents["folders"] != expected:
                        raise FileError(409, "確認した後にフォルダの中身が変わりました。もう一度確認してください")
                    # Descriptor-based removal: links inside are removed as links, never followed
                    shutil.rmtree(name, dir_fd=descriptor)
            elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                os.unlink(name, dir_fd=descriptor)  # removing a link removes the link itself, never its target
            else:
                raise FileError(403, "特殊ファイルは削除できません")
        return {"path": path}

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

    def pdf(self, path: str) -> bytes:
        with self.opened(path) as (descriptor, info):
            if info.st_size > PDF_LIMIT:
                raise FileError(413, "PDFは64 MiBまで表示できます")
            with os.fdopen(os.dup(descriptor), "rb") as stream:
                data = stream.read(PDF_LIMIT + 1)
        if len(data) > PDF_LIMIT:
            raise FileError(413, "PDFは64 MiBまで表示できます")
        if not data.startswith(b"%PDF-"):
            raise FileError(415, "PDFではありません")
        return data

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
