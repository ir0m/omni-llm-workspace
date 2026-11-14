"""Browser regression for Docs (requires a built web/, typst, tectonic and Chromium).

uvx --with playwright==1.56.0 python tests/browser_docs.py /tmp/omni-docs-ui
Uses a disposable app with no providers or agents; never connects to the user's server.
"""

from pathlib import Path
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def serve(folder: Path, port: int) -> None:
    sys.path.insert(0, str(ROOT))
    import uvicorn
    from omniws.config import Settings
    from omniws.main import create_app
    from omniws.providers.registry import Registry

    settings = Settings(
        _env_file=None,
        omni_data_dir=folder / "data",
        omni_projects_dir=folder / "projects",
        omni_password="pw",
        omni_allow_file_edit=True,
        omni_allow_network=False,
        omni_public_urls=f"http://127.0.0.1:{port}",
    )
    app = create_app(
        settings, web_dist=ROOT / "web/dist", registry=Registry(settings, {}), agents={}
    )
    uvicorn.run(app, host="127.0.0.1", port=port)


HOME_NOTE = "---\naliases: [ホーム]\ntags: [研究]\n---\n# Home\n\n[[Target|次のノート]] と [[Target#Section|節へ]] #実験\n\n> [!tip]- 折りたたみ\n> 内容 $x^2$\n\n![[Target#Section]]\n\n`[[not-link]]`\n\n```mermaid\ngraph TD\n A[Start] --> B[Done]\n```\n"
TARGET_NOTE = "---\naliases: [別名]\n---\n# Target\n\n## Section\n\n埋め込みの本文 telescope $x^2$。\n\n![[Home]]\n"


def check(base: str, artifacts: Path) -> None:
    from playwright.sync_api import sync_playwright, expect

    with sync_playwright() as p:
        b = p.chromium.launch()
        context = b.new_context(viewport={"width": 1440, "height": 950})
        page = context.new_page()
        errors = []
        context.on(
            "page", lambda tab: tab.on("pageerror", lambda e: errors.append(str(e)))
        )
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base)
        page.fill("input[type=password]", "pw")
        page.keyboard.press("Enter")
        page.get_by_role("button", name="Docs（文書の編集）").click()
        page.get_by_label("プロジェクト", exact=True).select_option("demo")
        page.get_by_role("button", name="Home.md", exact=True).click()
        expect(
            page.get_by_role("button", name="次のノート", exact=True)
        ).to_be_enabled()
        expect(page.locator(".note-callout")).to_have_count(1)
        expect(page.get_by_label("埋め込み: Target.md", exact=True)).to_contain_text(
            "埋め込みの本文"
        )
        expect(page.get_by_label("埋め込み: Home.md", exact=True)).to_contain_text(
            "循環参照"
        )
        expect(page.locator(".katex").first).to_be_attached()
        expect(page.locator('iframe[title="Mermaidの図"]')).to_be_visible(timeout=15000)
        page.screenshot(path=str(artifacts / "notes.png"))
        page.get_by_role("button", name="次のノート", exact=True).click()
        expect(page.locator(".cm-content")).to_contain_text("# Target")
        expect(page.get_by_role("list", name="バックリンク")).to_contain_text("Home.md")
        page.get_by_role("searchbox", name="ノートを検索").fill("telescope")
        expect(page.get_by_role("list", name="検索結果")).to_contain_text("Target.md")
        page.get_by_role("searchbox", name="ノートを検索").fill("")
        page.locator(".cm-content").click()
        page.keyboard.press("Control+End")
        page.keyboard.type("\n[[Hom")
        expect(page.locator(".cm-tooltip-autocomplete")).to_contain_text("Home")
        page.keyboard.press("Enter")
        page.keyboard.press("Control+s")
        expect(page.locator(".cm-content")).to_contain_text("[[Home]]")
        expect(page.get_by_label("未保存", exact=True)).to_have_count(0)
        page.get_by_role("button", name="slides.md", exact=True).click()
        expect(page.get_by_label("スライド 3", exact=True)).to_be_visible()
        page.get_by_role("button", name="発表", exact=True).click()
        dialog = page.get_by_role("dialog", name="発表者ビュー")
        expect(dialog.locator("footer")).to_contain_text("1 / 3")
        with page.expect_popup() as popup:
            page.get_by_role("button", name="投影用の画面を開く").click()
        audience = popup.value
        expect(audience.locator(".marpit h1")).to_have_text("題名")
        page.get_by_role("button", name="次のスライド", exact=True).click()
        expect(audience.locator(".marpit h2")).to_have_text("伝えたいこと")
        page.get_by_role("button", name="暗転（B）").click()
        expect(audience.locator(".marpit")).to_have_count(0)
        page.get_by_role("button", name="暗転（B）").click()
        expect(audience.locator(".marpit h2")).to_have_text("伝えたいこと")
        page.get_by_role("button", name="次のスライド", exact=True).click()
        expect(page.get_by_label("発表者ノート", exact=True)).to_contain_text(
            "ここに話す内容"
        )
        with page.expect_popup() as popup:
            page.get_by_role("button", name="PDFに保存・印刷").click()
        printing = popup.value
        expect(printing.locator(".print-slide")).to_have_count(3)
        expect(printing.locator(".print-slide").last.locator("h2")).to_have_text(
            "まとめ"
        )
        printing.pdf(
            path=str(artifacts / "slides.pdf"),
            prefer_css_page_size=True,
            print_background=True,
        )
        printing.close()
        page.screenshot(path=str(artifacts / "presenter.png"))
        page.get_by_role("button", name="発表者ビューを閉じる").click()
        audience.keyboard.press("ArrowLeft")
        expect(audience.locator(".marpit h2")).to_have_text("伝えたいこと")
        audience.close()
        for file in ["main.typ", "main.tex"]:
            page.get_by_role("button", name=file, exact=True).click()
            expect(
                page.get_by_label("プレビュー", exact=True).get_by_role("status")
            ).to_contain_text("コンパイル成功", timeout=20000)
            expect(page.locator("canvas")).to_be_visible()
            page.wait_for_timeout(500)
            pixels = page.locator("canvas").evaluate(
                '(c)=>{const a=c.getContext("2d").getImageData(0,0,c.width,c.height).data; let n=0; for(let i=0;i<a.length;i+=4) if(a[i]<180&&a[i+3]>0)n++; return n}'
            )
            assert pixels > 20, pixels
        page.set_viewport_size({"width": 390, "height": 844})
        page.get_by_role("button", name="表示", exact=True).click()
        expect(page.locator("canvas")).to_be_visible()
        page.wait_for_timeout(
            350
        )  # allow the sidebar transition and PDF resize to settle
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
        page.screenshot(path=str(artifacts / "mobile.png"))
        assert not errors, errors
        print(
            "PASS: notes, embeds, cycle guard, math, mermaid, search, backlinks, completion, Marp, notes, projection sync/blackout/disconnect, PDF export, actual Typst/TeX pixels, mobile; page errors:",
            errors,
        )
        b.close()


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--serve":
        serve(Path(sys.argv[2]), int(sys.argv[3]))
        return
    artifacts = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/omni-docs-ui")
    artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="omni-docs-test-") as temporary:
        folder = Path(temporary)
        project = folder / "projects/demo"
        project.mkdir(parents=True)
        (project / "Home.md").write_text(HOME_NOTE)
        (project / "Target.md").write_text(TARGET_NOTE)
        (project / "slides.md").write_text(
            (ROOT / "templates/document/docs/slides.md").read_text()
        )
        (project / "main.typ").write_text("= Title\nHello $x^2$")
        (project / "main.tex").write_text(
            r"\documentclass{article}\begin{document}Hello $x^2$\end{document}"
        )
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("OMNI_")
        }
        with (artifacts / "server.log").open("w") as log:
            server = subprocess.Popen(
                [
                    "uv",
                    "run",
                    "python",
                    str(Path(__file__).resolve()),
                    "--serve",
                    str(folder),
                    str(port),
                ],
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=log,
            )
            try:
                for _ in range(100):
                    if server.poll() is not None:
                        raise RuntimeError("Test server stopped: see server.log")
                    try:
                        with urllib.request.urlopen(base + "/api/health", timeout=1):
                            break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise RuntimeError("Test server did not start")
                check(base, artifacts)
            finally:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()


if __name__ == "__main__":
    main()
