# ロードマップ

根拠は [docs/research/2026-11-13-landscape.md](research/2026-11-13-landscape.md)。各フェーズは始める前に承認をもらい、終わったら報告する。

## 現在地
- P0 足場: 完了（`d7d0ac2`）
- P1 Chat 基盤: 完了（2026-11-13。本物のAPIキー・ローカルLLMでの確認は未実施）
- P2 エージェント層と Code: 完了（2026-11-13）
  - OpenCode（ACP）・Codex（app-server）・Claude Code（Agent SDK、サブスク／API課金）
  - 承認と `.env` 上限の権限判定、引き継ぎ、一時会話、使用率
  - 実機確認: OpenCode 無料モデル、Codex（Plus）、Claude Code（Pro）
  - 未確認: APIキー／ローカルLLMのモデルでのCode、Claude Code（API課金）、ブラウザ通知（HTTPSが必要）

## P1 Chat 基盤
依存: なし

- DB（SQLite）: Conversation、Turn（provider・model・agent・usage を記録）、Agent Session（native id・同期位置・ephemeral）、並び順・ピン留め・非表示、一時会話
- パスワードログイン
- 実行管理と SSE（ACP の `session/update` 形式に合わせたイベント、再接続対応）
- Chat アダプター: Anthropic SDK、OpenAI 互換（OpenAI / Gemini / OpenRouter / Ollama / LM Studio / llama.cpp / vLLM）、モデルの自動検出と capabilities
- 画面: 会話一覧（改名・ピン留め・並べ替え・非表示・分岐・削除・一時会話）、送信ごとのモデル切替、ターンごとの使用量

## P2 エージェント層と Code
依存: P1

- 汎用 ACP クライアント（stdio JSON-RPC）: OpenCode を最初の対象にする（API/ローカルモデルの Code を担当）
- ネイティブアダプター: Codex（app-server）、Claude Code（Agent SDK。既定は API キー）
- プロジェクト（`OMNI_PROJECTS_DIR`）の選択と作成
- 権限ポリシー（`.env` が上限）と承認カード
- Chat と Code、エージェント間の文脈引き継ぎ（Claude: 前置き、Codex: `inject_items`、ACP: `session/load` か前置き）

## P3 実用性
依存: P2

- 使用率とコンテキストの表示（Claude 5時間/7日、Codex 5時間/週、コンテキストメーター）
- 外部セッションの取り込みと操作
- ファイルと差分のパネル
- 端末（xterm.js＋PTY、WebSocket）
- git worktree による並列実行
- アカウント操作（状態の表示、`.env` で許可した Codex の操作）
- Chat での MCP ツール
- 追加のエージェント: agy（ACP）。Claude サブスク（CLI の既存ログインだけを使う。既定で無効）

## P4 配布
依存: P2（P3 と並行可）

- Dockerfile・compose（CLI のログインは volume に保存し、`docker compose exec` で各 CLI にログイン）
- systemd ユーザーユニット
- README、スクリーンショット

## 将来候補（P2以降）
- ACP エージェントの追加（Cursor CLI、Goose、Cline、Hermes、OpenClaw、OpenHands、Qwen Code）
- 実行結果の比較表示
- code-server へのリンク
- 画像の添付
- Responses API
- 共通の AGENTS.md

## 作らないもの
- 自前のエージェントループ（OpenCode で置き換える）
- 推論エンジン、モデル配信、IDE、端末エミュレーター
- 独自プロトコル
- RAG・文書管理
- LiteLLM の組み込み
- Claude.ai ログインの提供
- Cursor / Antigravity / Windsurf / Dots の非公式な自動化
