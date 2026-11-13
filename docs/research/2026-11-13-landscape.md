# 競合・OSS調査とアーキテクチャ再評価（2026-11-13）

OmniLLMWorkspace の方針を決めるための調査記録。確認日はすべて 2026-11-13。

**確度**
- 高: 公式ページで確認、または手元で実際に動かして確認
- 中: 公式ページの検索結果や、複数の二次情報が一致したもの
- 低: 二次情報だけのもの
- 未確認: 確かめられなかったもの

手元での検証に使った版: Claude Code 2.1.316、claude-agent-sdk 0.2.181、Codex CLI 0.166.0。

---

## 0. 要点

1. **エージェント接続の標準は ACP（Agent Client Protocol）**。公式の一覧には51のエージェントが載っている。
   - アダプター経由で接続するもの: Claude Code（`claude-agent-acp`）、Codex（`codex-acp`）。
   - 一覧に載っているもの: Gemini CLI、OpenCode、Goose、Cursor、Cline、Hermes Agent、OpenClaw、OpenHands など。
   - Antigravity CLI も `agy acp` で対応している。
   - したがって、エージェントごとに独自の接続を書くより、**ACPクライアントを1つ実装するのが基本**になる。（高）
2. **API/ローカルモデルのCodeモードに自前のエージェントループを作る計画は取り下げる**。代わりに、MITライセンスで任意のプロバイダーを使える OpenCode を ACP 経由で使う。ループ、ツール群、各モデルとの相性調整を自分で保守しなくて済む。
3. **Claudeサブスクの扱いを変える必要がある**。Anthropic の規約は、第三者の開発者が Claude.ai ログインを提供すること、Claude.ai の資格情報やトークンを「収集・保存・仲介」することを禁じている。例外は「改変していない Claude Code 本体に、本人が自分のサブスクでログインする」場合だけ。（高）
   - アプリは `CLAUDE_CODE_OAUTH_TOKEN` を持たない。
   - アプリからの Claude.ai ログインは作らない。
   - CLI 自身のログインだけを使う。この使い方は既定で無効にし、個人利用・自己責任と明記する。
4. **Gemini CLI は個人アカウントでの提供が 2026-06-18 に終了**した。個人の Google サブスクは Antigravity CLI（`agy`）を ACP で使う。ただし、アカウント課金のまま外部ツールから使ってよいかは**未確認**。
5. **最も近い競合は OpenHands Agent Canvas と Open WebUI v0.13**。
   - Agent Canvas: Claude Code・Codex・Gemini・任意の ACP エージェントを、ローカルの画面から複数の実行環境で動かす。
   - Open WebUI v0.13: 端末・ファイル・Git・サブエージェントを持つ。
   - OmniLLMWorkspace の独自の価値は次の4つ。
     - 1つの会話の中でチャット用モデルとエージェントを行き来し、文脈を引き継ぐこと
     - サブスクの使用率とコンテキストを一か所で見られること
     - `.env` を上限とする権限ポリシー
     - スマホから使うための日本語UI
6. **LiteLLM は組み込まない**。依存が重く、2026-03-24 に PyPI 版（1.82.7 / 1.82.8）が乗っ取られて、認証情報を盗むコードが配布された。必要なら、利用者が外部の OpenAI 互換プロキシとして立てる。（中〜高）

---

## 1. 競合・OSS比較表

ライセンスは配布や改変の条件に関わるので、必ず元のLICENSEファイルで確認すること。「—」は該当なし。

### 1.1 LLMチャット・統合UI

| 製品 | 役割 | コーディングエージェント | プロバイダー切替 | MCP / ACP | セルフホスト | ライセンス | 確度 |
|---|---|---|---|---|---|---|---|
| Open WebUI | 多機能チャットUI（RAG、ツール、v0.13で端末・ファイル・Git・サブエージェント） | Open Terminal で端末実行。外部エージェント接続の記述あり（細部は未確認） | Ollama・OpenAI互換 | MCP（標準は Streamable HTTP のみ。stdio は mcpo 経由）/ ACP 未確認 | 可 | Open WebUI License（BSD-3 + ブランド表示条項、OSI非承認） | 中 |
| LibreChat | マルチプロバイダーのチャット、エージェント、MCP | 自前のエージェント（コーディング専用ではない） | 多数 | MCP あり / ACP 未確認 | 可 | MIT（二次情報） | 中 |
| LobeChat / LobeHub | チャット、プラグイン、エージェント市場 | 未確認 | 多数 | MCP あり（二次情報） | 可 | LobeHub Community License（改変版の配布に制限） | 中 |
| AnythingLLM | ワークスペース単位のRAGとエージェント | なし | 多数 | MCP あり | 可（複数ユーザーはDockerのみ） | MIT | 中 |
| NextChat | 軽量なチャットUI | なし | 多数 | MCP（`ENABLE_MCP`） | 可 | MIT | 中 |

### 1.2 AIコーディングエージェント

| 製品 | 統合方法 | セッション再開 | 実行中の承認 | 使用量・上限 | セルフホスト / 外部依存 | ライセンス | 確度 |
|---|---|---|---|---|---|---|---|
| Claude Code | Agent SDK（Python/TS）、`claude -p`、ACPはアダプター `claude-agent-acp`（Apache-2.0） | 一覧・取得・分岐・改名・タグ・削除（SDK） | `can_use_tool` | ターン使用量、コンテキスト内訳、5時間/7日の使用率 | Anthropic API が必要 | CLI本体はプロプライエタリ。規約は §0-3 | 高（手元で確認） |
| Codex CLI | app-server（JSON-RPC、stdio/WS/Unix）、`codex exec --json`、Codex SDK、ACPはアダプター `codex-acp` | `thread/list`・read・fork・archive・delete・`inject_items` | `requestApproval` を受けて許可/拒否を返す | `account/rateLimits/read`、`tokenUsage` | OpenAI が必要 | Apache-2.0（記憶による。未取得） | 高（手元で確認） |
| Gemini CLI | `gemini --acp`、`-p --output-format stream-json` | ACP の範囲 | ACP | 未確認 | 個人アカウントは 2026-06-18 で終了 | Apache-2.0 | 高（終了の告知） |
| Antigravity CLI（agy） | `agy acp`、`-p --output-format json\|stream-json`、`--conversation <id>` | `--continue` / `--conversation` | ACP（詳細は未確認） | 未確認 | Google が必要。外部からのアカウント利用の可否は未確認 | プロプライエタリ（未確認） | 高（公式ドキュメント） |
| OpenCode | `opencode serve`（OpenAPI、既定ポート4096、Basic認証）、公式SDK、`opencode acp` | あり | あり | 未確認 | 任意のプロバイダー（ローカル含む） | MIT（未取得） | 高（公式ドキュメント） |
| Aider | CLI（Git中心） | 部分的 | なし（対話式） | — | 任意のモデル | Apache-2.0 | 中 |
| Cline | VS Code拡張とCLI。ACPの一覧に掲載 | — | — | — | 任意 | Apache-2.0 と MIT で情報が食い違う | 低 |
| Roo Code | VS Code拡張 | — | — | — | — | Apache-2.0。現状は未確認 | 未確認 |
| Continue | IDE拡張と `cn` CLI | — | — | — | 任意 | Apache-2.0。保守状況の情報が食い違う | 低 |
| Goose | デスクトップとCLI、MCPネイティブ、ACP対応 | — | — | — | 任意 | Apache-2.0。AAIF（Linux Foundation）へ移管 | 中 |

### 1.3 AI IDE・統合開発環境

| 製品 | 役割 | 公式の連携手段 | 分類 | 確度 |
|---|---|---|---|---|
| Cursor | プロプライエタリのAI IDE | CLI `agent acp`（JSON-RPC over stdio、`session/new` / `session/load`、承認あり）、`agent -p`、`CURSOR_API_KEY` | 一部機能のみ連携可（ACP） | 高 |
| Google Antigravity（IDE / デスクトップ / SDK） | プロプライエタリのエージェントIDE | CLIの ACP。Python SDK はリサーチプレビュー（二次情報） | 一部機能のみ連携可（CLIの ACP） | 中 |
| Windsurf（2026-06 に Devin Desktop へ改称と報道） | プロプライエタリIDE | 公式のAPI・CLIは見つからない | 参考製品 | 低 |
| OpenHands / Agent Canvas | エージェント実行基盤（Agent Server: REST）と、その操作画面（Canvas: OpenHands・Claude Code・Codex・Gemini・ACPエージェント、Docker/VM/Modal、定時実行） | Agent Server REST、ACP の一覧に掲載 | 参考製品（最も近い競合）。統合は将来候補 | 中 |
| code-server / OpenVSCode Server | ブラウザで使う VS Code | 外部ツールとして開く（リンク・リバースプロキシ） | 外部ツールとして接続（将来候補） | 中（MIT） |

### 1.4 常駐・自律型エージェント

| 製品 | 役割 | 分類 | 確度 |
|---|---|---|---|
| OpenAI Dots | ChatGPT の常駐エージェント（2026-09-29 発表、Pro / Business / Enterprise 向け、OpenAI 管理のクラウドPC） | 参考製品（公開APIは未確認） | 中 |
| OpenClaw | チャットアプリ（WhatsApp・Telegram・Discord等）とLLMをつなぐ常駐ゲートウェイ（Node.js）。ACPの一覧に掲載 | 参考製品、ACPで接続できる候補 | 中（MIT） |
| Hermes Agent（Nous Research） | 自己学習型の常駐エージェント、デスクトップ版あり。ACPの一覧に掲載 | 参考製品、ACPで接続できる候補 | 中（MIT） |

### 1.5 LLM接続・推論基盤

| 製品 | 役割 | 扱い | 確度 |
|---|---|---|---|
| Ollama / llama.cpp（llama-server）/ vLLM | ローカル推論。どれも OpenAI 互換の API を持つ | OpenAI互換アダプターで接続する。推論エンジンは作らない | 中（MIT / MIT / Apache-2.0） |
| OpenRouter | 多数のモデルを中継する SaaS | OpenAI互換アダプターで接続 | 中 |
| LiteLLM | SDKとプロキシ。100以上のプロバイダーを OpenAI 形式に統一 | 組み込まない。利用者が外部プロキシとして立てるのは可 | 中〜高（2026-03 の乗っ取り事件） |
| models.dev | モデルの仕様・価格の公開データベース（OpenCode が利用） | 任意のメタデータ源（キャッシュして使う） | 中（ライセンス未確認） |

### 1.6 プロトコル

| 名前 | 何か | 扱い |
|---|---|---|
| MCP | モデル・エージェントにツールやデータをつなぐ標準。AAIF（Linux Foundation）が管理 | Chat 用のツール源として使う。エージェントには ACP の `session/new` で MCP サーバーを渡す |
| ACP（Agent Client Protocol） | エディタ等のクライアントとコーディングエージェントの間の JSON-RPC（主に stdio）。`session/list` と `usage_update` は 2026 年に安定版になった。`session/set_model` は未確認（不安定扱いの情報あり） | エージェント接続の基本。IBM の Agent Communication Protocol（A2A に統合）とは別物 |
| CLI固有のプロトコル（Codex app-server、Claude Agent SDK） | ACP より多くの機能（使用率、セッション操作、`inject_items`、コンテキスト内訳）を持つ | ネイティブアダプターとして、ACP では足りない部分にだけ使う |
| SSE / WebSocket | ブラウザへのリアルタイム配信 | 会話のストリームは SSE（再接続可）、端末は WebSocket |

---

## 2. 対応モデル・ツール一覧

| 対象 | 区分 | 統合方式 | 状況 |
|---|---|---|---|
| Anthropic API | P0 | 既存SDK（`anthropic`） | 未実装（P1） |
| OpenAI API | P0 | OpenAI互換アダプター（httpx。Responses API は P2） | 未実装（P1） |
| Gemini API（APIキー） | P1 | OpenAI互換エンドポイント（独自機能が必要になれば `google-genai`） | 未実装 |
| OpenRouter / Ollama / LM Studio / llama.cpp / vLLM | P0 | OpenAI互換アダプター＋モデル自動検出（`/v1/models`、Ollama `/api/tags`） | 未実装（P1） |
| Codex（ChatGPTプラン） | P0 | ネイティブ（app-server） | 手元で検証済み、未実装 |
| Claude Code（APIキー） | P0 | ネイティブ（Agent SDK、`ANTHROPIC_API_KEY`） | 手元で検証済み、未実装 |
| Claude Code（サブスク） | P1 | CLIの既存ログインを使うだけ。アプリは資格情報を持たない。既定で無効 | 規約の制約あり（§0-3） |
| OpenCode | P0 | ACP（`opencode acp`）。API/ローカルモデルのCodeモードを担当 | 未実装 |
| Antigravity CLI（agy） | P1 | ACP（`agy acp`） | 未インストール。アカウント課金での外部利用の可否は未確認 |
| Gemini CLI（APIキー / 企業プラン） | P2 | ACP（`gemini --acp`） | 個人アカウントは終了済み |
| Cursor CLI / Goose / Cline / Hermes / OpenClaw / OpenHands / Qwen Code 等 | P2 | 汎用ACPクライアントに設定を足すだけ | — |
| Aider | 不要 | ACP 非対応で対話式。OpenCode と役割が重なる | — |
| Cursor IDE / Antigravity IDE / Windsurf / Dots | 不要（参考のみ） | 公式の連携手段がない、またはIDE本体 | — |
| code-server | P2 | 外部ツールとして開く | — |
| MCP（Chat用ツール） | P1 | 既存SDK（公式 `mcp` Python SDK） | 未実装 |

モデル名は書き込まない。プロバイダーとエージェントから一覧を取り、能力（画像入力・ツール・構造化出力・ストリーミング・コンテキスト長・推論設定）を `capabilities` として持つ。取れない値は models.dev や利用者の設定で補う。

---

## 3. 既存OSSとの機能比較

| 製品 | 既にできること | 足りないこと（このアプリの目的に対して） | OmniLLMWorkspaceで作る価値 | 作る必要がないこと |
|---|---|---|---|---|
| Open WebUI | 多数のプロバイダーとのチャット、RAG、ツール、MCP、端末・ファイル・Git（v0.13）、サブエージェント、RBAC | サブスクCLI（Claude Code / Codex）のネイティブなセッション・承認・使用率。外部で作ったセッションの取り込み。1つの会話でのエージェント間の文脈引き継ぎ | サブスクCLIとACPエージェントを中心にした統一と引き継ぎ、使用率・コンテキストの一覧表示、`.env` を上限とする権限 | RAG、文書管理、RBAC、画像生成、音声 |
| OpenCode | 任意のプロバイダーで動くコーディングエージェント、HTTPサーバーとSDK、ACP | チャットUI全体、ほかのエージェントとの統合、サブスクの使用率 | OpenCode を1つのエージェントとして載せること | 自前のエージェントループとツール群 |
| OpenHands Agent Canvas | 複数エージェント（ACP含む）の画面操作、複数の実行環境、定時実行、Webhook | チャット用モデルとの統一、サブスクの使用率、日本語・スマホ向け | 会話中心の統一体験と文脈の引き継ぎ | 実行環境の管理（Modal 等）、自動化の機能群 |
| LibreChat | マルチプロバイダーのチャット、エージェント、MCP、Artifacts | コーディングエージェントの統合 | — | エージェントビルダー、コードインタプリタ |
| LiteLLM | プロバイダーの統一、コスト計算、プロキシ | — | — | 組み込み（外部プロキシとして接続できれば十分） |
| code-server | ブラウザで使うIDE | — | — | IDEとエディタ本体（必要ならリンクで開く） |

**Agent Canvas について**: Code 側の機能は大きく重なる。P2 に入る前に30分ほど実際に触り、どこが違うか（会話の統一、引き継ぎ、使用率、スマホ）を確かめることを勧める。

---

## 4. ギャップ分析（コミット `d7d0ac2` 時点）

| 機能 | 状態 | 関連ファイル | 次のアクション |
|---|---|---|---|
| 設定の読み込み（`.env`、空値=未設定、`~` 展開） | 実装済み | `omniws/config.py` | 権限キーの追加（P2） |
| Claudeの資格情報の項目 | 改善が必要 | `omniws/config.py`（`claude_code_oauth_token`）、`.env.example` | 規約に合わせて削除し、CLIの既存ログインだけを使う（要判断） |
| Codexの資格情報の項目 | 改善が必要 | `codex_auth_json_b64` | Dockerでは `docker compose exec` で `codex login` を案内する方式に変え、項目を削除 |
| ホスト名制限、SPA配信、ヘルスチェック、起動 | 実装済み | `omniws/main.py`、`omniws/__main__.py` | そのまま使う |
| フロントの雛形（Tailwind トークン、ダーク/ライト） | 部分実装 | `web/src/*` | P1 で画面を作る |
| ログイン | 未実装 | — | P1 |
| 会話、ターン、バックエンドセッション、一時会話のDB | 未実装 | — | P1（§5のモデルで作る） |
| 実行管理と SSE | 未実装 | — | P1 |
| Chatアダプター（Anthropic、OpenAI互換） | 未実装 | 流用元: `~/dev/YourFeed/yourfeed/llm/*` | P1 |
| エージェント層（ACPクライアント、Codex app-server、Claude Agent SDK） | 未実装（検証済み） | 検証コードはスクラッチパッドのみ | P2 |
| 自前のエージェントループ（旧計画） | 不要 | — | 作らない。OpenCode（ACP）で置き換える |
| 権限ポリシー（`.env` が上限） | 未実装 | — | P2（ACP の `session/request_permission`、Codex の承認要求、Claude の `can_use_tool` を1つの判定器へ） |
| プロジェクト管理、worktree、ファイル/差分パネル、端末 | 未実装 | — | P2 / P3 |
| Docker / systemd | 未実装 | 流用元: YourFeed の Dockerfile・compose | P4 |

---

## 5. アーキテクチャ評価

### 用語（混同しないこと）

| 概念 | 定義 |
|---|---|
| Provider | 推論の接続先とその資格情報（Anthropic API、OpenAI API、ある URL の Ollama、OpenRouter） |
| Model | Provider が出すモデル。`capabilities` を持つ |
| Agent | 自分でループとツールを持つ外部の実行体（Claude Code、Codex、OpenCode、agy …）。ACP かネイティブアダプターで接続する。Agent が選ばせるモデルは Provider のモデルとは別に扱う |
| Conversation | アプリ側の会話（DB）。ターンの列で、各ターンは Model（Chat）か Agent（Code）が実行する。一時会話はアプリにだけ保存し、Agent 側は記録を残さない（ephemeral / no-session-persistence。手元で確認） |
| Session | Agent 側の本来のセッション（Claude の session_id、Codex の thread_id、ACP の sessionId）。Conversation に紐づけ、どこまで見せたかを記録する |
| Workspace | `OMNI_PROJECTS_DIR` 内のプロジェクト（並列実行時は git worktree） |
| Runtime | Agent のプロセスが動く場所（ホスト、Docker）と、その sandbox・ポリシー |
| Tool | ターン中に使う能力（MCP ツール、Agent の内蔵ツール、ACP のクライアント機能 fs/terminal） |

### 維持するもの
- 単一の Python バックエンド（FastAPI）＋ React SPA、SQLite、SSE＋REST、127.0.0.1＋Tailscale Serve、パスワードログイン
- Agent の承認を1つのポリシー判定器に集める設計
- アプリが会話の記録を持ち、Agent 側のネイティブセッションは「再開できるなら再開、できなければ記録から再投入」とする二段構え。異なる製品の間でセッションを完全に移すことはしない

### 改善するもの
- **イベント形式は ACP の `session/update` に合わせる**（`agent_message_chunk`、`agent_thought_chunk`、`tool_call`、`tool_call_update`、`plan`、`usage_update`）。ACP にないもの（`rate_limit`、`context_usage`）だけを拡張として足す。Chat モデルのストリームも同じ形に直す
- **Agent 層 = ACP クライアント＋ネイティブアダプター**。共通の操作（開始・送信・中断・承認・再開・一覧）は ACP の形に合わせる。製品固有の機能（Codex の使用率・`inject_items`、Claude のコンテキスト内訳・タグ）は、能力として宣言する拡張に置く
- **API/ローカルモデルの Code は OpenCode に任せる**（ACP）。資格情報は環境変数で渡す
- **アカウント操作**: 状態の表示は可。Codex のログイン・ログアウト・設定書き込み・リセットクレジットは `.env` で操作ごとに許可する。Claude.ai のログインはアプリで提供しない（規約）

### 変えないもの
- 推論エンジン、モデル配信、IDE、端末エミュレーター（xterm.js を使う）、認証・暗号の自作はしない
- 独自プロトコルは作らない

---

## 参照

- Anthropic の規約（Claude Code）: https://code.claude.com/docs/en/legal-and-compliance （高）
- ACP のエージェント一覧: https://agentclientprotocol.com/get-started/agents （高）
- ACP の usage_update 安定化: https://agentclientprotocol.com/announcements/session-usage-stabilized （中）
- Codex App Server: https://developers.openai.com/codex/app-server （→ https://learn.chatgpt.com/docs/app-server 、高）
- OpenCode server / ACP: https://opencode.ai/docs/server 、https://opencode.ai/docs/cli/acp/ （高）
- Gemini CLI ACP / 個人アカウント終了: https://geminicli.com/docs/cli/acp-mode/ 、https://developers.google.com/gemini-code-assist/docs/deprecations/code-assist-individuals （高）
- Antigravity CLI: https://antigravity.google/docs/cli/acp 、https://antigravity.google/docs/cli/headless （高）。外部からの利用可否についての議論: https://discuss.ai.google.dev/t/is-external-orchestration-of-antigravity-cli-headless-mode-supported-with-account-based-usage/183051
- Cursor CLI ACP: https://cursor.com/docs/cli/acp.md （中）
- Claude Agent ACP アダプター: https://zed.dev/blog/claude-code-via-acp （中）
- Open WebUI: https://docs.openwebui.com/license/ 、https://docs.openwebui.com/features/mcp 、https://openwebui.com/blog/v0-13-0-your-model-gets-a-computer （中）
- LibreChat 2026 roadmap: https://librechat.ai/blog/2026-02-18_2026_roadmap （中）
- OpenHands Agent Canvas: https://openhands.dev/product/canvas 、https://docs.openhands.dev/openhands/usage/agent-canvas （中）
- OpenHands SDK: https://github.com/OpenHands/software-agent-sdk （中）
- Goose / AAIF: https://block.xyz/inside/block-anthropic-and-openai-launch-the-agentic-ai-foundation （中）
- OpenAI Dots: https://techcrunch.com/2026/09/29/openai-launches-dots-its-bubbly-agentic-avatar/ （中）
- Hermes Agent: https://hermes-agent.nousresearch.com/desktop （中）
- LiteLLM の乗っ取り: https://research.jfrog.com/post/litellm-compromised-teampcp/ （中〜高）
- models.dev: https://github.com/anomalyco/models.dev （中）
