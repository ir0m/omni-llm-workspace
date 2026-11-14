"""Templates: folders of text files that are copied into projects.

A template is nothing more than a folder. The ones under `templates/` in this repository are small examples and
read-only. The user's own are ordinary projects in the TEMPLATE_GROUP group under OMNI_PROJECTS_DIR, so they are
written and edited like any other project: in Docs, or by an agent in Code. A user template replaces the
bundled one with the same name.

TEMPLATE.md describes a template (first heading: its label, first paragraph: its description) and is never copied.
`{{name}}` and `{{date}}` in a file are replaced when it is copied into a project.
"""

from __future__ import annotations

import datetime
import difflib
import os
import stat
from pathlib import Path

from . import projects
from .config import ROOT, Settings
from .files import EDIT_LIMIT, FileError, ProjectFiles, visible

BUILTIN_DIR = ROOT / "templates"
TEMPLATE_GROUP = "_templates"
INFO_FILE = "TEMPLATE.md"
MAX_FILES = 200
DIFF_LIMIT = 64 * 1024
STARTER = "# {label}\n\nこのテンプレートの説明を書きます。この TEMPLATE.md はプロジェクトにはコピーされません。\n\n" \
          "このフォルダに置いたファイルが、そのままプロジェクトに追加されます。\n" \
          "ファイルの中の `{{{{name}}}}` はプロジェクト名、`{{{{date}}}}` は追加した日付に置き換わります。\n"


class TemplateError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message
        super().__init__(message)


def read_folder(root: Path) -> tuple[dict[str, str], list[str]]:
    """The UTF-8 text files of a folder as {relative path: text}, and the paths left out (binary, too large).
    Links are never followed, and what the file browser hides (secrets, .git) is not part of a template."""
    found: dict[str, str] = {}
    skipped: list[str] = []
    for folder, names, files in os.walk(root, followlinks=False):
        relative = Path(folder).relative_to(root).parts
        names[:] = sorted(name for name in names if visible((*relative, name)) and not (Path(folder) / name).is_symlink())
        for name in sorted(files):
            parts = (*relative, name)
            path = "/".join(parts)
            if not visible(parts) or name == projects.GROUP_MARKER:
                continue
            try:
                info = os.lstat(Path(folder) / name)
                if not stat.S_ISREG(info.st_mode):
                    continue
                if info.st_size > EDIT_LIMIT:
                    skipped.append(path)
                    continue
                data = (Path(folder) / name).read_bytes()
                if b"\0" in data:
                    raise UnicodeDecodeError("utf-8", b"", 0, 1, "binary")
                found[path] = data.decode("utf-8")
            except UnicodeDecodeError:
                skipped.append(path)
            except OSError:
                continue
            if len(found) > MAX_FILES:
                raise TemplateError(413, f"テンプレートに入れられるファイルは{MAX_FILES}個までです")
    return found, skipped


def describe(name: str, info: str | None) -> tuple[str, str]:
    """(label, description) from TEMPLATE.md."""
    label, description = name, []
    seen_heading = False
    for line in (info or "").splitlines():
        text = line.strip()
        if text.startswith("#") and not seen_heading:
            label, seen_heading = text.lstrip("#").strip() or name, True
        elif text and not text.startswith("#"):
            description.append(text)
        elif description:
            break
    return label, " ".join(description)[:300]


def user_name(name: str) -> str:
    try:
        parts = projects.segments(name)
    except projects.ProjectError as exc:
        raise TemplateError(400, "テンプレート名が正しくありません（/ と先頭の . は使えません）") from exc
    if len(parts) != 1:
        raise TemplateError(400, "テンプレート名に / は使えません")
    return parts[0]


def user_root(settings: Settings, name: str) -> Path | None:
    project = f"{TEMPLATE_GROUP}/{user_name(name)}"
    if not projects.is_project(settings.omni_projects_dir, project):
        return None
    try:
        return ProjectFiles(settings, project).root
    except FileError:
        return None


def template_names(settings: Settings) -> dict[str, str]:
    """{name: "user" | "builtin"}: the user's own templates first take the place of bundled ones."""
    names: dict[str, str] = {}
    if BUILTIN_DIR.is_dir():
        for entry in sorted(BUILTIN_DIR.iterdir()):
            if entry.is_dir() and not entry.is_symlink() and not entry.name.startswith("."):
                names[entry.name] = "builtin"
    group = settings.omni_projects_dir / TEMPLATE_GROUP
    if projects.is_group(group):
        for entry in sorted(group.iterdir()):
            if entry.is_dir() and projects.is_project(settings.omni_projects_dir, f"{TEMPLATE_GROUP}/{entry.name}"):
                names[entry.name] = "user"
    return names


def load(settings: Settings, name: str) -> dict:
    name = user_name(name)
    source = template_names(settings).get(name)
    if source is None:
        raise TemplateError(404, "テンプレートが見つかりません")
    root = user_root(settings, name) if source == "user" else BUILTIN_DIR / name
    if root is None:
        raise TemplateError(404, "テンプレートが見つかりません")
    files, skipped = read_folder(root)
    label, description = describe(name, files.pop(INFO_FILE, None))
    return {"name": name, "source": source, "label": label, "description": description,
            "project": f"{TEMPLATE_GROUP}/{name}" if source == "user" else None,
            "files": files, "skipped": skipped}


def summary(template: dict) -> dict:
    return {**{key: template[key] for key in ("name", "source", "label", "description", "project", "skipped")},
            "files": sorted(template["files"])}


def list_templates(settings: Settings) -> list[dict]:
    result = []
    for name in template_names(settings):
        try:
            result.append(summary(load(settings, name)))
        except TemplateError as exc:
            result.append({"name": name, "source": "user", "label": name, "description": exc.message,
                           "project": f"{TEMPLATE_GROUP}/{name}", "files": [], "skipped": []})
    result.sort(key=lambda item: (item["source"] != "user", item["label"].casefold()))
    return result


def render(text: str, project: str) -> str:
    return text.replace("{{name}}", project.rsplit("/", 1)[-1]).replace("{{date}}", datetime.date.today().isoformat())


def plan(settings: Settings, name: str, project: str) -> dict:
    """What adding the template to the project would do, file by file. Nothing is written."""
    template = load(settings, name)
    target = ProjectFiles(settings, project)
    entries = []
    for path, raw in sorted(template["files"].items()):
        text = render(raw, project)
        entry = {"path": path, "status": "new", "hash": None, "diff": None}
        try:
            current = target.file(path, EDIT_LIMIT)
            if current["kind"] != "text" or current["truncated"]:
                entry["status"] = "blocked"
            elif current["text"] == text:
                entry["status"] = "same"
            else:
                diff = "".join(difflib.unified_diff(current["text"].splitlines(keepends=True), text.splitlines(keepends=True),
                                                    fromfile=f"a/{path}", tofile=f"b/{path}"))
                entry.update(status="changed", hash=current["hash"], diff=diff[:DIFF_LIMIT])
        except FileError as exc:
            if exc.status != 404:
                entry["status"] = "blocked"
        entries.append(entry)
    return {"template": summary(template), "project": project, "files": entries}


def write_new(target: ProjectFiles, path: str, text: str, base_hash: str | None = None) -> None:
    parts = path.split("/")
    for depth in range(1, len(parts)):
        try:
            target.make_folder("/".join(parts[:depth]))
        except FileError as exc:
            if exc.status != 409:  # the folder is already there
                raise
    target.write(path, text, base_hash, create=base_hash is None)


def apply(settings: Settings, name: str, project: str, items: list[dict]) -> dict:
    """Writes the chosen files. A file that exists is replaced only while it still has the content the plan showed."""
    template = load(settings, name)
    target = ProjectFiles(settings, project)
    results = []
    for item in items:
        path = item["path"]
        if path not in template["files"]:
            results.append({"path": path, "ok": False, "message": "テンプレートにないファイルです"})
            continue
        try:
            write_new(target, path, render(template["files"][path], project), item.get("base_hash"))
            results.append({"path": path, "ok": True, "message": None})
        except FileError as exc:
            results.append({"path": path, "ok": False, "message": exc.message})
    return {"files": results}


def fill_new_project(settings: Settings, name: str, project: str) -> None:
    """Every file of the template into a project that was just made (files it already has are kept)."""
    fresh = [{"path": entry["path"]} for entry in plan(settings, name, project)["files"] if entry["status"] == "new"]
    apply(settings, name, project, fresh)


def create(settings: Settings, name: str, source: str | None) -> dict:
    """A new template of the user's own: empty, a copy of another template, or the text files of a project
    (`source` is "template:<name>" or "project:<name>")."""
    name = user_name(name)
    files: dict[str, str] = {}
    if source and source.startswith("template:"):
        origin = load(settings, source.removeprefix("template:"))
        files = dict(origin["files"])
        files[INFO_FILE] = f"# {origin['label']}\n\n{origin['description']}\n"
    elif source and source.startswith("project:"):
        try:
            files, _ = read_folder(ProjectFiles(settings, source.removeprefix("project:")).root)
        except FileError as exc:
            raise TemplateError(exc.status, exc.message) from exc
        files.pop(INFO_FILE, None)
    elif source:
        raise TemplateError(400, "コピー元の指定が正しくありません")
    files.setdefault(INFO_FILE, STARTER.format(label=name))
    project = f"{TEMPLATE_GROUP}/{name}"
    try:
        projects.create_project(settings.omni_projects_dir, project, False, False, False)
    except projects.ProjectError as exc:
        if exc.code == "exists":
            raise TemplateError(409, "同じ名前のテンプレートが既にあります") from exc
        raise TemplateError(400, exc.message or "テンプレートを作成できませんでした") from exc
    target = ProjectFiles(settings, project)
    for path, text in files.items():
        write_new(target, path, text)
    return summary(load(settings, name))
