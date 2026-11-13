# AGENTS.md — OmniLLMWorkspace の作業ルール

このリポジトリで作業するエージェント（Codex、Claude Code、OpenCode など）向けの指示です。最初に `docs/handoff.md`（現状と次の作業）と `docs/roadmap.md` を読んでください。

## 何のアプリか

API・サブスクリプション・ローカルLLMを1つの画面で切り替え、プロジェクトに依存しない **Chat** と、プロジェクトのフォルダで作業する **Code** を同じ会話の中で行き来できる、セルフホスト型のWebアプリです。

- 自宅サーバーで常駐し、スマホから Tailscale Serve（HTTPS）経由で使います。
- 利用者は1人です。返答とUIの文言は日本語にします。

## 進め方（必ず守る）

- 機能やUIは、**具体的・詳細に質問しながら**実装する。
  - 見た目の選択肢はモックアップ付きで、1回に最大4問程度。
  - 自分だけで決めて黙って作らない。
- **フェーズ（または段階）ごとに**、計画を示して承認をもらってから実装する。終わったら、変更点・実行したコマンド・テスト結果を短く報告する。
- **コミットとpushは、ユーザーに確認してから**行う（`git@github.com:ir0m/omni-llm-workspace.git`、ブランチ main）。
  - コミットメッセージは英語で、何を・なぜを書く。
- `.env`・`data/`・鍵・トークンの値は**表示しない、コミットしない**。設定済みかどうかだけを報告する。
- 個人用なので、過剰な確認や注意書きはしない。
- 大きな破壊的変更や、重要な機能の削除が必要なときは、実行せずに理由・影響・代わりの案を先に報告する。

## このアプリの上で作業するときの注意

このリポジトリは、常駐しているこのアプリ自身のプロジェクト（`~/omni-projects/OmniLLMWorkspace`）として登録されています。

- `systemctl --user restart omni-llm-workspace` を**自分で実行しない**。今動いている自分のターンも止まります。変更を常駐版に反映するときは、ユーザーに頼む。
- 動作確認は別ポート・別データで起動する。

```sh
OMNI_PORT=8301 OMNI_DATA_DIR=/tmp/omni-dev OMNI_PASSWORD=dev OMNI_PUBLIC_URLS=http://localhost:8301 uv run python -m omniws
```

## 構成

- バックエンド: Python 3.12、FastAPI、uv、SQLite（SQLAlchemy 2 + Alembic）
  - `omniws/main.py`: アプリの組み立て（ホスト名と Origin の制限、SPAの配信）
  - `omniws/auth.py`: パスキー（WebAuthn）。`OMNI_PASSWORD` は開発用
  - `omniws/runs.py`: 返答をバックグラウンドで走らせ、SSEで配信する（Last-Event-IDで再開、承認待ち）
  - `omniws/blocks.py`: 更新を返答のブロックにまとめる規則。`web/src/blocks.ts` と同じ規則に保つ
  - `omniws/providers/`: Chat 用（Anthropic SDK、OpenAI互換）
  - `omniws/agents/`: エージェント
    - `opencode.py`: ACP
    - `codex.py`: app-server
    - `claude.py`: Agent SDK
    - `policy.py`: 権限の判定
    - `handover.py`: 引き継ぎ
    - `jsonrpc.py`, `acp.py`: 共通の通信部品
  - `omniws/api/`: REST
  - `omniws/projects.py`: プロジェクト（`OMNI_PROJECTS_DIR` 直下のフォルダ）
- フロント: React 19、Vite 8、TypeScript 7、Tailwind 4（`web/`）
- 更新イベントの形は ACP の `session/update` に合わせる。独自のプロトコルは増やさない。

## 守る設計（理由は docs/research/2026-11-13-landscape.md）

- **Claude.ai の資格情報はアプリで扱わない**（Anthropic の規約）。サブスクは CLI 自身のログインだけを使い、`OMNI_ALLOW_CLAUDE_SUBSCRIPTION=true` のときだけ有効にする。
  - 子プロセスには、`ANTHROPIC_*` と `CLAUDE*` を空にして渡す。
- **権限は `.env` が上限**。判定の順番は、`.env` の拒否 → 会話の「常に許可」→ 承認モード（ask / auto_edit / auto）→ ユーザーに確認。
  - つないだコマンド（`;` `&&` `|` `>` `$(` など）は「常に許可」で覚えない。
- エージェントの設定ファイルの許可ルールで、アプリの承認を迂回させない。
  - Claude は `setting_sources=[]` にして CLAUDE.md だけを渡す。
  - Codex は `untrusted` にする。
  - OpenCode は permission を `ask` にする。
- 自前のエージェントループ、推論エンジン、IDE、端末エミュレーターは作らない。

## よく使うコマンド

```sh
uv run pytest -q                        # バックエンドのテスト（全部通すこと）
(cd web && npm run build)               # 型チェックとビルド
uv run python -m omniws                 # 起動（127.0.0.1:8300）
uv run python -m omniws passkey-link    # パスキー登録リンク（15分・1回限り）
OMNI_DATA_DIR=/tmp/x uv run alembic revision --autogenerate -m "..." --rev-id 000N   # マイグレーション
python3 scripts/mock_openai.py 18555    # E2E用のOpenAI互換モック（LOCAL_OPENAI_BASE_URL=http://127.0.0.1:18555/v1）
```

## 画面の確認

- Playwright のヘッドレスChromium（`~/.cache/ms-playwright/chromium_headless_shell-1194/*/headless_shell`）を `executable_path` に指定して操作し、スクリーンショットを撮る。
  - スクリプトは作業用の一時フォルダに置く。
- パスキーは、CDP の `WebAuthn.addVirtualAuthenticator` で試せる。
- 本物のサブスク（Codex、Claude）で試すのは最小限にする。Claude は Haiku。テストで作ったCLI側のセッションは消しておく。

## 環境の注意

- agent ユーザーは docker グループに入っていない（Docker は `docker compose config` での確認まで）。
- ポート 8000 は YourFeed、Tailscale Serve の 443 と 8443 は使用中。このアプリは 8300 で、外向きは 10000。
- サーバーを止めるときは PID で止める。`pkill -f` は自分のシェルに当たることがある。
- Ubuntu 24.04 の AppArmor が権限のないユーザー名前空間を制限している（`kernel.apparmor_restrict_unprivileged_userns=1`）。
  - そのため、Codex の**ネットワークを遮断するサンドボックス**（read-only、または `OMNI_ALLOW_NETWORK=false`）では、コマンドが `bwrap: loopback: Failed RTM_NEWADDR` で失敗する。
  - アプリの Code モードは既定（ネットワーク許可）なら動く。直すには `docs/handoff.md` の手順（sudo）が必要。
