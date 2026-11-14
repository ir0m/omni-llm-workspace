import stat

import pytest

from omniws import compile as compiler

FAKE_TYPST = """#!/bin/sh
# compile --root ROOT --diagnostic-format short SOURCE OUT
if grep -q BROKEN "$6"; then echo "$(basename "$6"):3:1: error: unknown variable: foo"; exit 1; fi
if grep -q SLOW "$6"; then sleep 30; fi
printf '%%PDF-1.7 built from ' > "$7"; cat "$6" >> "$7"
"""
FAKE_TECTONIC = """#!/bin/sh
# -X compile --untrusted --outdir DIR SOURCE
for last; do :; done
if grep -q BROKEN "$last"; then echo "error: $(basename "$last"):4: Undefined control sequence"; exit 1; fi
printf '%%PDF-1.5 tex %s' "$*" > "$5/$(basename "$last" .tex).pdf"
"""


@pytest.fixture
def project(client, tmp_path, monkeypatch):
    tools = tmp_path / "bin"
    tools.mkdir()
    for name, script in (("typst", FAKE_TYPST), ("tectonic", FAKE_TECTONIC)):
        (tools / name).write_text(script)
        (tools / name).chmod(0o755)
    monkeypatch.setattr(compiler, "find_tool", lambda name: str(tools / name))
    assert client.post("/api/projects", json={"name": "app", "git_init": False}).status_code == 201
    root = client.app.state.settings.omni_projects_dir / "app"
    (root / "paper").mkdir()
    return root


def build(client, path):
    return client.post("/api/projects/app/compile", json={"path": path})


def test_typst_builds_outside_the_project_and_keeps_the_last_good_pdf(client, project):
    (project / "paper" / "main.typ").write_text("= Title\n")
    assert client.get("/api/projects/app/pdf", params={"path": "paper/main.typ"}).status_code == 404
    result = build(client, "paper/main.typ").json()
    assert result["ok"] and result["engine"] == "typst" and result["has_pdf"] and result["diagnostics"] == []
    pdf = client.get("/api/projects/app/pdf", params={"path": "paper/main.typ"})
    assert pdf.headers["content-type"] == "application/pdf" and pdf.content.startswith(b"%PDF-1.7 built from = Title")
    assert sorted(entry.name for entry in (project / "paper").iterdir()) == ["main.typ"]  # nothing written beside it

    (project / "paper" / "main.typ").write_text("= Title\nBROKEN\n#foo\n")
    failed = build(client, "paper/main.typ").json()
    assert not failed["ok"] and failed["has_pdf"]
    assert failed["diagnostics"] == [{"path": "paper/main.typ", "line": 3, "severity": "error", "message": "unknown variable: foo"}]
    assert b"BROKEN" not in client.get("/api/projects/app/pdf", params={"path": "paper/main.typ"}).content


def test_latex_runs_untrusted_and_offline_when_the_network_is_off(client, project):
    (project / "paper" / "main.tex").write_text("\\documentclass{article}\n")
    assert build(client, "paper/main.tex").json()["ok"]
    built = client.get("/api/projects/app/pdf", params={"path": "paper/main.tex"}).content
    assert b"--untrusted" in built and b"--only-cached" not in built
    client.app.state.settings.omni_allow_network = False
    assert build(client, "paper/main.tex").json()["ok"]
    assert b"--only-cached" in client.get("/api/projects/app/pdf", params={"path": "paper/main.tex"}).content
    (project / "paper" / "main.tex").write_text("BROKEN\n")
    failed = build(client, "paper/main.tex").json()
    assert failed["diagnostics"][0] == {"path": "paper/main.tex", "line": 4, "severity": "error", "message": "Undefined control sequence"}


def test_compile_is_limited_to_documents_the_browser_may_read(client, project, tmp_path):
    outside = tmp_path / "outside.typ"
    outside.write_text("secret")
    (project / "link.typ").symlink_to(outside)
    (project / "notes.md").write_text("x")
    (project / ".env.typ").write_text("x")
    assert build(client, "link.typ").status_code == 403
    assert build(client, "../outside.typ").status_code == 403
    assert build(client, ".env.typ").status_code == 403
    assert build(client, "notes.md").status_code == 415
    assert build(client, "missing.typ").status_code == 404
    client.app.state.settings.omni_allow_compile = False
    (project / "a.typ").write_text("x")
    assert build(client, "a.typ").status_code == 403


def test_a_compiler_that_hangs_is_stopped(client, project, monkeypatch):
    monkeypatch.setattr(compiler, "TIMEOUT", 1)
    (project / "slow.typ").write_text("SLOW\n")
    result = build(client, "slow.typ").json()
    assert not result["ok"] and not result["has_pdf"] and "中止" in result["diagnostics"][0]["message"]


def test_missing_compiler_is_reported(client, project, monkeypatch):
    monkeypatch.setattr(compiler, "find_tool", lambda name: None)
    (project / "a.typ").write_text("x")
    response = build(client, "a.typ")
    assert response.status_code == 503 and "typst" in response.json()["detail"]
    assert client.get("/api/config").json()["compilers"] == {"typst": False, "tex": False}


def test_project_pdfs_are_served_and_other_files_are_not(client, project):
    (project / "slides.pdf").write_bytes(b"%PDF-1.4 slides")
    (project / "fake.pdf").write_text("<html>")
    assert client.get("/api/projects/app/file", params={"path": "slides.pdf"}).json()["kind"] == "pdf"
    served = client.get("/api/projects/app/pdf", params={"path": "slides.pdf"})
    assert served.content == b"%PDF-1.4 slides" and served.headers["x-content-type-options"] == "nosniff"
    assert client.get("/api/projects/app/pdf", params={"path": "fake.pdf"}).status_code == 415
    assert client.get("/api/projects/app/pdf", params={"path": ".git/x.pdf"}).status_code == 403
    assert stat.S_IMODE((project / "slides.pdf").stat().st_mode)  # untouched


def test_invalid_compiler_output_does_not_replace_a_good_pdf(client, project, monkeypatch):
    (project / 'a.typ').write_text('= Good')
    assert build(client, 'a.typ').json()['ok']
    before = client.get('/api/projects/app/pdf', params={'path': 'a.typ'}).content

    def invalid(command, cwd, work, network):
        (work / 'out.pdf').write_bytes(b'not a PDF')
        return 0, ''

    monkeypatch.setattr(compiler, 'run', invalid)
    result = build(client, 'a.typ').json()
    assert not result['ok'] and result['has_pdf']
    assert client.get('/api/projects/app/pdf', params={'path': 'a.typ'}).content == before
