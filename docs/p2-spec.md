# P2 仕様: Code モードとエージェント層（2026-11-13 合意）

質問への回答で決めた内容。小さな段階に分けて実装し、各段階で気になる点はその都度質問する。

## 画面

- **モード切替**: ヘッダーの `[Chat | Code]`。会話の途中でも切り替えられる。Code では 📁 プロジェクト選択が現れる。スマホではモードと実行元をヘッダーの2段目に置く
- **プロジェクト**: ヘッダーの選択欄。`OMNI_PROJECTS_DIR` 直下のフォルダを一覧し、ブランチと未コミットの変更数を表示する。末尾に「＋ 新しいフォルダ（git init 可）」「⤓ git clone…」（`.env` で許可したときだけ）
- **会話一覧**: 「📌 ピン留め」（プロジェクトに関係なく先頭）→「チャット」→ プロジェクトごとのグループ（折りたためる）。並べ替えはグループの中で行う
- **実行元→モデルの2段選択**:
  - 実行元は、サブスク（Codex、Claude Code）・API・ローカルに分けて並べる
  - サブスクには使用率（Claude 5時間/7日、Codex 5時間/週）を添える
  - Code で API・ローカルのモデルを選ぶと、OpenCode が動かす（その旨を注記）
  - 思考の深さは、モデルが対応していれば3つ目の選択欄に出す
- **ツール表示**:
  - 1行カードで出し、押すと展開する
  - 連続した読み取りは「📄 3ファイルを読んだ」にまとめる
  - 編集は `+12 −3` で、展開すると差分を出す
  - コマンドは成否と時間で、展開すると出力を出す
- **承認カード**:
  - 入力欄のすぐ上に固定して出す。何を・どこで（コマンド、パス、差分）を見せる
  - ボタンは［許可］［この会話では常に許可］［拒否…］。拒否では理由を書いてエージェントに伝えられる
- **通知**: 承認待ちと応答完了をブラウザ通知とタブのタイトルで知らせる（HTTPS、つまり Tailscale Serve 経由で有効）
- **引き継ぎの表示**: 実行元を変えたとき、会話に「→ Claude Code に引き継ぎ（発言12件・操作要約）」の区切りを入れる

## 実行元（エージェント）

| 実行元 | 接続 | Chat | Code | 認証 |
|---|---|---|---|---|
| Codex | app-server（JSON-RPC、常駐1プロセス） | 読み取り専用・承認なし | sandbox `workspace-write`。承認要求はすべてアプリの判定器へ | CLI のログイン（`codex login`） |
| Claude Code | Agent SDK（`ClaudeSDKClient`） | `tools=[]` | 内蔵ツール。`can_use_tool` をアプリの判定器へ | `ANTHROPIC_API_KEY`、または CLI のログイン（`OMNI_ALLOW_CLAUDE_SUBSCRIPTION=true` のときだけ。画面に自己責任の注記） |
| OpenCode | ACP クライアント（`opencode acp`） | 使わない（API・ローカルは直接つなぐ） | API・ローカルのモデル。モデルは `configOptions` で指定 | 環境変数の API キー。ログイン不要の無料モデルもある |

- CLI の子プロセスには必要な環境変数だけを渡す。Claude のサブスクを使うときは `ANTHROPIC_API_KEY` を渡さない
- **一時会話**: Claude は `--no-session-persistence` と `CLAUDE_CODE_DISABLE_AUTO_MEMORY`、Codex は `ephemeral` で memories と履歴保存を無効にする（両方とも手元で確認済み）。OpenCode は会話を閉じたらセッションを削除する（実装時に確認）

## 引き継ぎ

- エージェントごとのネイティブセッションに、それが見た最後の発言を記録する（`agent_sessions.synced_seq`）
- 次のターンでは、まだ見ていない発言を自動で全部渡す。ツール実行は「何をしてどうなったか」の要約にする
  - Claude Code: 最初のプロンプトの前置き
  - Codex: `thread/inject_items`
  - OpenCode: 前置き
- 同じエージェントの中でのモデル変更はネイティブに切り替える（Claude `set_model`、Codex `turn/start` の model、OpenCode の configOptions）

## 権限（`.env` が上限）

| キー | 既定 | 意味 |
|---|---|---|
| `OMNI_APPROVAL_DEFAULT` | `auto_edit` | 新しい会話の承認モード: `ask`（読み取り以外は確認）/ `auto_edit`（プロジェクト内の編集は自動、コマンドとプロジェクト外は確認）/ `auto`（拒否パターン以外は自動） |
| `OMNI_APPROVAL_MAX` | `auto` | GUI で選べる一番緩いモード |
| `OMNI_ALLOW_SHELL` | `true` | `false` ならコマンド実行は常に拒否 |
| `OMNI_SHELL_DENY` | `sudo,su ,rm -rf /,mkfs,shutdown,reboot,git push --force` | 含まれていたら常に拒否（カンマ区切り） |
| `OMNI_ALLOW_CLAUDE_SUBSCRIPTION` | `false` | Claude Code を CLI のログイン（サブスク）で使う |
| `OMNI_ALLOW_PROJECT_CREATE` / `OMNI_ALLOW_GIT_CLONE` | `true` / `true` | プロジェクトの作成と clone |

- 「この会話では常に許可」は、会話ごとに「種類（編集・コマンド等）＋コマンドの先頭語」で記憶する
- 判定の順番: `.env` の拒否 → 会話の記憶 → 承認モード → 確認

## データ

- `conversations`: `approval_mode`、`allow_rules`（JSON）を追加。`project` を使い始める
- `messages.content` にブロックを追加
  - `{"type": "tool_call", "id", "kind": "read|edit|execute|search|fetch|other", "title", "status", "locations", "diff", "output"}`
  - `{"type": "handover", "agent", "count"}`
- 実行元の一覧: `/api/executors`（プロバイダーとエージェントを2段選択の形で返す。サブスクの使用率を含む）

## 実装の順番

1. プロジェクト API とヘッダー（Chat|Code、📁）、一覧のグループ化、2段選択の外枠
2. エージェント層の共通部分（ACP 形式の更新、承認要求、判定器）と OpenCode（ACP）→ 無料モデルで実機確認
3. Codex（app-server）→ 実機確認
4. Claude Code（Agent SDK）→ 実機確認（使用率 96% のため Haiku で最小限）
5. ツールカード、承認カード、通知、引き継ぎ、一時会話
6. テスト、画面確認、報告
