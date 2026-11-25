"""Files attached to chat messages.

Each upload is kept under OMNI_DATA_DIR/attachments/<id>/ as `data` and `meta.json`. A stored message refers
to it by id ({"type": "attachment", "id", "name", "media_type", "size", "kind"}), so the database and the
providers' replay checkpoints never hold file bytes. The bytes are read again each time a request is built.

Kinds: image (PNG, JPEG, GIF, WebP, by their signature), pdf, text (UTF-8 up to TEXT_LIMIT) and file (anything
else, which only agents in Code can use, as a copy in the project).
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import time
import unicodedata
from pathlib import Path
from typing import Any

FILE_LIMIT = 20 * 1024 * 1024
IMAGE_LIMIT = 5 * 1024 * 1024  # the strictest provider limit for one image (Claude API)
TEXT_LIMIT = 1024 * 1024  # larger UTF-8 files are attached as plain files
MAX_PER_MESSAGE = 10
MESSAGE_LIMIT = 50 * 1024 * 1024
UNSENT_TTL = 24 * 60 * 60
ID = re.compile(r"^[0-9a-f]{32}$")
PROJECT_FOLDER = ".omni-attachments"
KIND_LABEL = {"image": "画像", "pdf": "PDF", "text": "テキスト", "file": "ファイル"}


class AttachmentError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message
        super().__init__(message)


def root(data_dir: Path) -> Path:
    return data_dir / "attachments"


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


def clean_name(name: str | None) -> str:
    name = unicodedata.normalize("NFC", (name or "").replace("\\", "/").rsplit("/", 1)[-1])
    name = "".join(char for char in name if unicodedata.category(char)[0] != "C").strip().lstrip(".")
    if len(name.encode()) > 200:
        stem, dot, suffix = name.rpartition(".")
        suffix = suffix if dot and len(suffix) <= 10 else ""
        name = (stem or name).encode()[:180].decode(errors="ignore").rstrip() + (f".{suffix}" if suffix else "")
    return name or "file"


def classify(data: bytes, name: str) -> tuple[str, str]:
    """(kind, media type) from the content. What the browser claimed is never trusted."""
    found = image_type(data)
    if found:
        return "image", found
    if data.startswith(b"%PDF-"):
        return "pdf", "application/pdf"
    if len(data) <= TEXT_LIMIT and b"\0" not in data:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:
            guessed = mimetypes.guess_type(name)[0] or ""
            return "text", guessed if guessed.startswith("text/") else "text/plain"
    return "file", "application/octet-stream"


def save(data_dir: Path, data: bytes, name: str | None, generated: bool = False) -> dict[str, Any]:
    """`generated`: an image a model made. It is only shown and downloaded, never sent to a provider as input,
    so the per-image input limit does not apply."""
    if not data:
        raise AttachmentError(400, "空のファイルは添付できません")
    if len(data) > FILE_LIMIT:
        raise AttachmentError(413, "添付できるファイルは20 MiBまでです")
    name = clean_name(name)
    kind, media_type = classify(data, name)
    if kind == "image" and len(data) > IMAGE_LIMIT and not generated:
        raise AttachmentError(413, "画像は5 MiBまで添付できます")
    meta = {"id": secrets.token_hex(16), "name": name, "media_type": media_type, "size": len(data), "kind": kind,
            "created_at": int(time.time())}
    folder = root(data_dir) / meta["id"]
    folder.mkdir(parents=True)
    (folder / "data").write_bytes(data)
    (folder / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return meta


def folder(data_dir: Path, attachment_id: str) -> Path:
    if not ID.match(attachment_id or ""):
        raise AttachmentError(404, "添付ファイルが見つかりません")
    return root(data_dir) / attachment_id


def meta(data_dir: Path, attachment_id: str) -> dict[str, Any]:
    try:
        return json.loads((folder(data_dir, attachment_id) / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AttachmentError(404, "添付ファイルが見つかりません。もう一度添付してください") from exc


def path(data_dir: Path, attachment_id: str) -> Path:
    return folder(data_dir, attachment_id) / "data"


def read(data_dir: Path, attachment_id: str) -> bytes:
    try:
        return path(data_dir, attachment_id).read_bytes()
    except OSError as exc:
        raise AttachmentError(404, "添付ファイルが見つかりません") from exc


def block(item: dict[str, Any]) -> dict[str, Any]:
    """The reference a stored message keeps."""
    return {"type": "attachment", **{key: item[key] for key in ("id", "name", "media_type", "size", "kind")}}


EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}


def save_generated(data_dir: Path, data: bytes, prompt: str | None = None) -> dict[str, Any]:
    """An image a model generated, kept like an attachment of the reply (shown, downloadable, cleaned up with it)."""
    media_type = image_type(data)
    if media_type is None:
        raise AttachmentError(415, "生成された画像の形式を認識できませんでした")
    item = save(data_dir, data, f"generated-{time.strftime('%Y%m%d-%H%M%S')}.{EXTENSIONS[media_type]}", generated=True)
    result = {**block(item), "generated": True}
    if prompt:
        result["prompt"] = prompt[:4000]
    return result


def resolve(data_dir: Path, ids: list[str]) -> list[dict[str, Any]]:
    if len(ids) > MAX_PER_MESSAGE:
        raise AttachmentError(400, f"添付できるファイルは1回に{MAX_PER_MESSAGE}個までです")
    items = [meta(data_dir, attachment_id) for attachment_id in dict.fromkeys(ids)]
    if sum(item["size"] for item in items) > MESSAGE_LIMIT:
        raise AttachmentError(413, "添付ファイルは1回に合計50 MiBまでです")
    return items


def size_label(size: int) -> str:
    return f"{size / 1024 / 1024:.1f} MiB" if size >= 1024 * 1024 else f"{max(size, 1) / 1024:.0f} KiB" if size >= 1024 else f"{size} B"


def text_part(name: str, text: str) -> str:
    return f"<attachment name={json.dumps(name, ensure_ascii=False)}>\n{text}\n</attachment>"


def note(item: dict[str, Any], reason: str) -> str:
    return f"[添付ファイル {item.get('name')}（{KIND_LABEL.get(item.get('kind', ''), 'ファイル')}）: {reason}]"


# --- Requests to API models --------------------------------------------------------------------------------------

def expand(messages: list[dict[str, Any]], data_dir: Path | None, style: str, kinds: set[str]) -> list[dict[str, Any]]:
    """Messages with attachment references replaced by the provider's own parts. `style`: anthropic, chat or
    responses. A kind the provider can't take (or a file that's gone) becomes a short note, so a conversation
    that moved to another provider keeps working."""
    if not any(isinstance(message.get("content"), list) and any(part.get("type") == "attachment"
               for part in message["content"] if isinstance(part, dict)) for message in messages):
        return messages
    result = []
    for message in messages:
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            result.append(message)
            continue
        parts = []
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "attachment":
                parts.append(text_style(part, style))
                continue
            parts.append(convert(part, data_dir, style, kinds))
        result.append({**message, "content": parts})
    return result


def text_style(part: Any, style: str) -> Any:
    if style == "responses" and isinstance(part, dict) and part.get("type") == "text":
        return {"type": "input_text", "text": part.get("text", "")}
    return part


def convert(item: dict[str, Any], data_dir: Path | None, style: str, kinds: set[str]) -> dict[str, Any]:
    kind = item.get("kind")
    if kind not in kinds:
        return text_style({"type": "text", "text": note(item, "このモデルには送れない形式のため省略")}, style)
    try:
        if data_dir is None:
            raise AttachmentError(404, "")
        data = read(data_dir, item["id"])
    except AttachmentError:
        return text_style({"type": "text", "text": note(item, "ファイルが見つからないため省略")}, style)
    name, media_type = item.get("name", "file"), item.get("media_type", "application/octet-stream")
    if kind == "text":
        return text_style({"type": "text", "text": text_part(name, data.decode("utf-8", errors="replace"))}, style)
    encoded = base64.b64encode(data).decode()
    if style == "anthropic":
        if kind == "image":
            return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": encoded}}
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": encoded},
                "title": name}
    url = f"data:{media_type};base64,{encoded}"
    if style == "responses":
        if kind == "image":
            return {"type": "input_image", "image_url": url}
        return {"type": "input_file", "filename": name, "file_data": url}
    if kind == "image":
        return {"type": "image_url", "image_url": {"url": url}}
    return {"type": "file", "file": {"filename": name, "file_data": url}}


# --- Agents ------------------------------------------------------------------------------------------------------

def git_exclude(project: Path) -> None:
    """Keep the copies out of `git status` without touching the project's own .gitignore."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
    try:
        result = subprocess.run(["git", "--no-optional-locks", "-C", str(project), "-c", "core.fsmonitor=false",
                                 "-c", f"core.hooksPath={os.devnull}", "rev-parse", "--path-format=absolute",
                                 "--git-path", "info/exclude"], capture_output=True, text=True, env=env, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return
    if result.returncode or not result.stdout.strip():
        return
    exclude = Path(result.stdout.strip())
    line = f"/{PROJECT_FOLDER}/"
    try:
        current = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
        if line not in current.splitlines():
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with exclude.open("a", encoding="utf-8") as stream:
                stream.write(("" if not current or current.endswith("\n") else "\n") + line + "\n")
    except OSError:
        pass


def copy_into(project: Path, data_dir: Path, items: list[dict[str, Any]]) -> list[str]:
    """Copies the files into <project>/.omni-attachments/<id>/<name> and returns their paths in the project.
    Never follows a link an agent may have put there."""
    paths = []
    base = os.open(project, os.O_RDONLY | os.O_DIRECTORY)
    try:
        holder = open_folder(base, PROJECT_FOLDER)
        try:
            for item in items:
                short = item["id"][:12]
                inner = open_folder(holder, short)
                try:
                    try:
                        target = os.open(item["name"], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644,
                                         dir_fd=inner)
                    except FileExistsError:
                        target = None  # the same attachment, copied for an earlier turn
                    if target is not None:
                        with os.fdopen(target, "wb") as out, path(data_dir, item["id"]).open("rb") as source:
                            shutil.copyfileobj(source, out)
                finally:
                    os.close(inner)
                paths.append(f"{PROJECT_FOLDER}/{short}/{item['name']}")
        finally:
            os.close(holder)
    finally:
        os.close(base)
    git_exclude(project)
    return paths


def open_folder(parent: int, name: str) -> int:
    try:
        os.mkdir(name, 0o755, dir_fd=parent)
    except FileExistsError:
        pass
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)


def listing(paths: list[str], items: list[dict[str, Any]]) -> str:
    lines = [f"- {where}（{KIND_LABEL[item['kind']]}、{size_label(item['size'])}）" for where, item in zip(paths, items)]
    return "[添付ファイル（プロジェクト内に保存済み）]\n" + "\n".join(lines)


# --- Cleanup -----------------------------------------------------------------------------------------------------

def referenced(contents: list[list[dict[str, Any]]]) -> set[str]:
    return {block.get("id") for content in contents for block in content or []
            if isinstance(block, dict) and block.get("type") == "attachment"}


def collect(data_dir: Path, keep: set[str], now: float | None = None) -> int:
    """Removes files no message refers to. Uploads younger than UNSENT_TTL are kept: they may be in a draft."""
    now = time.time() if now is None else now
    removed = 0
    try:
        entries = list(os.scandir(root(data_dir)))
    except FileNotFoundError:
        return 0
    for entry in entries:
        if not ID.match(entry.name) or entry.name in keep or not entry.is_dir(follow_symlinks=False):
            continue
        try:
            if now - entry.stat(follow_symlinks=False).st_mtime < UNSENT_TTL:
                continue
        except OSError:
            continue
        shutil.rmtree(entry.path, ignore_errors=True)
        removed += 1
    return removed
