"""Typst and LaTeX documents to PDF, on the server.

The compilers run on a bounded snapshot in a restricted child process, never in the live project.
Typst cannot run programs; Tectonic runs with --untrusted, which turns shell escape off. The built PDF of a
document is kept until the next successful build, so a failed build still leaves the last good one to look at.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .config import Settings
from .files import FileError, ProjectFiles, parts_of

ENGINES = {".typ": "typst", ".tex": "tectonic"}
TIMEOUT = 120  # Tectonic downloads its TeX files the first time a package is used
LOG_LIMIT = 64 * 1024
PDF_LIMIT = 64 * 1024 * 1024
MAX_DIAGNOSTICS = 100
EXTRA_PATHS = ("~/.local/bin", "~/.cargo/bin")
TYPST_LINE = re.compile(r"^(?P<file>[^:\n]+):(?P<line>\d+):(?P<column>\d+): (?P<severity>error|warning): (?P<message>.*)$")
TECTONIC_LINE = re.compile(r"^(?P<severity>error|warning): (?P<file>[^:\n]+):(?P<line>\d+): (?P<message>.*)$")

running = threading.BoundedSemaphore(2)
locks = [threading.Lock() for _ in range(64)]


class CompileError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message
        super().__init__(message)


def find_tool(name: str) -> str | None:
    """The service's PATH is often shorter than a login shell's: also look where user installs usually go."""
    found = shutil.which(name)
    if found:
        return found
    for folder in EXTRA_PATHS:
        candidate = Path(folder).expanduser() / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def available() -> dict[str, bool]:
    return {"typst": find_tool("typst") is not None, "tex": find_tool("tectonic") is not None}


def engine_for(path: str) -> str:
    engine = ENGINES.get(Path(path).suffix.lower())
    if engine is None:
        raise CompileError(415, "コンパイルできるのは .typ と .tex のファイルです")
    return engine


def build_dir(settings: Settings, root: Path, path: str) -> Path:
    key = hashlib.sha256(f"{root}\0{path}".encode()).hexdigest()[:32]
    return settings.omni_data_dir / "builds" / key


def source(settings: Settings, project: str, path: str) -> tuple[Path, Path]:
    """(project root, source file), after the same checks as reading the file in the browser."""
    try:
        files = ProjectFiles(settings, project)
        with files.opened(path):
            pass
        return files.root, files.root.joinpath(*parts_of(path))
    except FileError as exc:
        raise CompileError(exc.status, exc.message) from exc


def diagnostics(engine: str, output: str, root: Path, folder: Path) -> list[dict]:
    pattern = TYPST_LINE if engine == "typst" else TECTONIC_LINE
    found = []
    for line in output.splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        file = Path(match["file"])
        absolute = (file if file.is_absolute() else folder / file).resolve()
        found.append({"path": absolute.relative_to(root).as_posix() if absolute.is_relative_to(root) else None,
                      "line": int(match["line"]), "severity": match["severity"],
                      "message": match["message"].removeprefix("! ").strip()})
        if len(found) >= MAX_DIAGNOSTICS:
            break
    return found


def run(command: list[str], cwd: Path, work: Path, network: bool) -> tuple[int | None, str]:
    """(exit code or None when it ran out of time, the end of what the compiler printed)."""
    cache = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    cache_paths = [cache / name for name in ("tectonic", "typst", "fontconfig")]
    for directory in cache_paths:
        directory.mkdir(parents=True, exist_ok=True)
    env = {key: os.environ[key] for key in ("PATH", "LANG", "TYPST_FONT_PATHS") if key in os.environ}
    env.update(HOME=str(work / "home"), TMPDIR=str(work / "tmp"), XDG_CACHE_HOME=str(cache), LANG="C.UTF-8")
    for directory in (work / "home", work / "tmp"):
        directory.mkdir(exist_ok=True)
    policy = {"read": ["/usr", "/lib", "/lib64", "/bin", "/etc/fonts", "/etc/ssl/certs", "/etc/pki",
                       "/etc/ld.so.cache", "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf",
                       command[0], *env.get("TYPST_FONT_PATHS", "").split(os.pathsep)],
              "write": [str(work), *map(str, cache_paths)],
              "execute": [command[0], "/usr/bin", "/bin", "/lib", "/lib64"],
              "network": network, "file_limit": PDF_LIMIT, "timeout": TIMEOUT}
    policy["read"] = [value for value in policy["read"] if value]
    command = [sys.executable, "-I", str(Path(__file__).with_name("compile_worker.py")), json.dumps(policy), *command]
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=output,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code: int | None = process.wait(TIMEOUT)
        except subprocess.TimeoutExpired:
            code = None
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        size = output.seek(0, os.SEEK_END)
        output.seek(max(0, size - LOG_LIMIT))
        return code, output.read().decode("utf-8", errors="replace")


def compile_document(settings: Settings, project: str, path: str) -> dict:
    if not settings.omni_allow_compile:
        raise CompileError(403, "コンパイルは無効です。OMNI_ALLOW_COMPILE=true の設定が必要です")
    engine = engine_for(path)
    tool = find_tool(engine)
    if tool is None:
        raise CompileError(503, f"{engine} がサーバーにインストールされていません")
    root, file = source(settings, project, path)
    target = build_dir(settings, root, path)
    lock = locks[int(target.name, 16) % len(locks)]
    if not running.acquire(timeout=TIMEOUT):
        raise CompileError(503, "ほかのコンパイルが終わるのを待ちましたが、時間内に始められませんでした")
    try:
        with lock:
            target.mkdir(parents=True, exist_ok=True)
            work = Path(tempfile.mkdtemp(dir=target))
            try:
                snapshot = work / "source"
                snapshot.mkdir()
                try:
                    snapshot_project(ProjectFiles(settings, project), snapshot)
                except FileError as exc:
                    raise CompileError(exc.status, exc.message) from exc
                file = snapshot.joinpath(*parts_of(path))
                if engine == "typst":
                    produced = work / "out.pdf"
                    command = [tool, "compile", "--root", str(snapshot), "--diagnostic-format", "short", str(file), str(produced)]
                else:
                    produced = work / f"{file.stem}.pdf"
                    command = [tool, "-X", "compile", "--untrusted", "--outdir", str(work),
                               *([] if settings.omni_allow_network else ["--only-cached"]), str(file)]
                started = time.monotonic()
                code, log = run(command, file.parent, work, settings.omni_allow_network)
                seconds = round(time.monotonic() - started, 2)
                ok = code == 0 and produced.is_file()
                valid_pdf = False
                if ok and produced.stat().st_size <= PDF_LIMIT:
                    with produced.open("rb") as stream:
                        valid_pdf = stream.read(5) == b"%PDF-"
                if ok and not valid_pdf:
                    ok = False
                    log += "\nPDFの形式または64 MiBの上限を確認してください"
                if ok:
                    os.replace(produced, target / "out.pdf")
            finally:
                shutil.rmtree(work, ignore_errors=True)
    finally:
        running.release()
    found = diagnostics(engine, log, snapshot, file.parent)
    if code is None:
        found.insert(0, {"path": None, "line": None, "severity": "error", "message": f"{TIMEOUT}秒以内に終わらなかったため中止しました"})
    return {"ok": ok, "engine": engine, "seconds": seconds, "log": log, "diagnostics": found,
            "has_pdf": (target / "out.pdf").is_file()}


def built_pdf(settings: Settings, project: str, path: str) -> Path:
    engine_for(path)
    root, _ = source(settings, project, path)
    pdf = build_dir(settings, root, path) / "out.pdf"
    if not pdf.is_file():
        raise CompileError(404, "まだコンパイルされていません")
    if pdf.stat().st_size > PDF_LIMIT:
        raise CompileError(413, "PDFは64 MiBまで表示できます")
    return pdf


def snapshot_project(files: ProjectFiles, target: Path) -> None:
    """Copy only files exposed by the browser; no links, private paths or unbounded trees."""
    pending, total, count = [""], 0, 0
    while pending:
        folder = pending.pop()
        offset = 0
        while True:
            tree = files.tree(folder, offset, 500)
            for entry in tree["entries"]:
                count += 1
                if count > 10000:
                    raise CompileError(413, "コンパイル対象は10000項目までです")
                relative = entry["path"]
                if entry["kind"] == "directory":
                    if entry["name"] in {"node_modules", ".venv", "__pycache__", ".obsidian"}:
                        continue
                    if relative.count("/") > 32:
                        raise CompileError(413, "フォルダの階層が深すぎます")
                    (target / relative).mkdir()
                    pending.append(relative)
                elif entry["kind"] == "file":
                    with files.opened(relative) as (descriptor, info):
                        remaining = 128 * 1024 * 1024 - total
                        if info.st_size > remaining:
                            raise CompileError(413, "コンパイル対象は合計128 MiBまでです")
                        with os.fdopen(os.dup(descriptor), "rb") as stream:
                            data = stream.read(remaining + 1)
                        total += len(data)
                        if total > 128 * 1024 * 1024:
                            raise CompileError(413, "コンパイル対象は合計128 MiBまでです")
                        (target / relative).write_bytes(data)
            offset = tree["next_offset"]
            if offset is None:
                break
