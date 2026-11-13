# 引き継ぎメモ（Claude Code → Codex など）

最終更新: 2026-11-13（Codex）。作業ルールは `AGENTS.md`、全体の計画は `docs/roadmap.md` を参照。

## 今回の続き: P3の外部セッション

- ユーザーは **外部セッション取り込みとファイル・差分パネルの両方** を選択。詳細は `docs/p3-spec.md`。
- Codex / Claude Code のセッションを、既存会話と混ぜてラベルを追加せず一覧化する実装を追加。開いたときの履歴同期と元のセッションでの再開に対応。
- 一覧／会話の更新ボタン、読み取り失敗の表示、削除した会話の再出現防止を追加。改名等の操作はアプリ内だけに作用する。
- DBマイグレーション `0004_external_sessions.py`。既存DBの更新は起動時。常駐版は再起動していない。
- 検証: バックエンド **99件成功**、型チェック・ビルド成功（既知の500 kB超の警告）。ビルド先は `/tmp/omni-p3-web-dist` で、常駐版の `web/dist` は更新していない。
- 8301番・別DB・偽エージェントでPlaywright確認。390pxと1440px、ラベルなしの一覧と履歴表示、ブラウザ例外なし。スクリプトと画像は `/tmp/omni-p3-check/`。
- 実CLIは読み取りのみ確認: Codexは一覧と履歴取得成功。Claudeはこのプロジェクトの対象履歴が0件だったため、履歴変換・再開はSDKモックで検証。本物のモデルには送信していない。
- **次はファイル・差分パネルを実装する。** 初期案（遅延読み込みツリー、テキスト／画像の閲覧、未コミット差分全体、PC右側／スマホシート）について、ユーザーから実装指示を受けた。
- ユーザーは外部セッションまでの現状のコミット・push・反映を承認済み。その後、技術選定の説明とファイル・差分パネルの実装を進める。常駐反映の再起動はユーザーが行う。

## 今どこまでできているか

- **P0〜P2 は完了。** 中身は次のとおり。
  - Chat
  - Code（OpenCode / Codex / Claude Code）
  - 承認と `.env` 上限の権限
  - 引き継ぎ
  - 一時会話
  - 使用率
- **ログインはパスキー（WebAuthn）。**
  - 最初の端末は `uv run python -m omniws passkey-link` のリンクから登録する。
  - `OMNI_PASSWORD` は開発用。
- **常駐:** systemd ユーザーユニット `omni-llm-workspace`（`deploy/systemd/`）が `127.0.0.1:8300` で動いている。
  - 外向きは Tailscale Serve の `https://irom-desktop.tail500274.ts.net:10000`。
  - 公開には一度だけ `sudo tailscale serve --bg --https=10000 http://127.0.0.1:8300` が必要。
  - このリポジトリは `~/omni-projects/OmniLLMWorkspace`（シンボリックリンク）としてアプリから選べる。
- 検証: `uv run pytest -q` は **83件成功**。フロントの型チェック・ビルド成功（500 kB超のチャンク警告あり）。
  - このマシンの Node/npm は `~/.local/opt/node/bin`。PATHにない場合は `PATH="$HOME/.local/opt/node/bin:$PATH" npm run build` を `web/` で実行する。
  - 別ポート8301・一時DB・Playwrightの仮想認証器で、スマホ幅390pxの「登録 → ログアウト → 再ログイン」を確認。ブラウザ例外なし。
  - 旧ログインCookieの拒否、登録完了時のリンク有効期限と一回限りの消費、不正なWebAuthn入力のエラー処理を修正。

## アプリへ移る前に残っている操作

- 常駐サービスは起動中・自動起動も有効。ただし今回のバックエンド修正を読み込むには、ユーザーが `systemctl --user restart omni-llm-workspace` を実行する。
- 2026-11-13の確認時点で Tailscale Serve の10000番は未設定。sudoのパスワードが必要なので、ユーザーが上記の `sudo tailscale serve ...` を実行する。
- その後 `uv run python -m omniws passkey-link` を実行し、表示された登録リンクを、Tailscale接続中のスマホで開く。リンクのトークンはチャットや資料に転記しない。
- パスキー登録後は Code → プロジェクト `OmniLLMWorkspace` → Codex を選び、この資料から続きを進める。
- AppArmorの問題は未修復。下記sudo手順もまだ実行していない。このCodexセッションでも通常のサンドボックス実行が同じエラーで失敗し、許可されたサンドボックス外のコマンドで検証した。

## 実機で確認できていないこと

- APIキーやローカルLLMのモデルを使ったCode（OpenCode 経由）、Claude Code（API課金）。APIキーやローカルLLMがまだない。
- ブラウザ通知（HTTPS が必要）。
- 上限時の「別の実行元で続ける」。
- 作業計画バー（実機のモデルがToDoツールを使わなかった）。
- Docker（agent ユーザーが docker グループ外）。

## 環境の既知の問題: Codex のネットワーク遮断サンドボックス

- 何が起きるか: Ubuntu 24.04 の AppArmor の制限で、Codex がネットワークを切ったサンドボックスを作れず、コマンドが `bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted` で失敗する。
- 影響を受ける場面: `codex exec --sandbox read-only`、`OMNI_ALLOW_NETWORK=false` にしたときのアプリの Code、ターミナルで既定設定の `codex`。
- アプリの Code モード（既定のネットワーク許可）は影響を受けない。
- 直すには、ユーザーが sudo で `/usr/bin/bwrap` に AppArmor プロファイルを足す。

```sh
sudo tee /etc/apparmor.d/bwrap >/dev/null <<'EOF2'
abi <abi/4.0>,
include <tunables/global>

profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
  include if exists <local/bwrap>
}
EOF2
sudo apparmor_parser -r /etc/apparmor.d/bwrap
```

## P3の候補（外部セッションの現在地は上記を参照）

候補（`docs/roadmap.md` の P3）と、最初に聞くとよいことを並べる。

1. **外部セッションの取り込みと操作**
   - 何をするか: ターミナルなどで作った Claude Code / Codex のセッションを、プロジェクトの一覧に出し、続きから再開・改名・非表示・分岐・削除できるようにする。
   - 使える仕組み: Claude は Agent SDK の `list_sessions` / `get_session_messages` / `rename_session` / `tag_session` / `fork_session` / `delete_session`。Codex は app-server の `thread/list`（cwd で絞る）・`thread/read`・`thread/name/set`・`thread/archive`・`thread/fork`・`thread/delete`。
   - 聞くこと: 一覧での見せ方（アプリの会話と混ぜるか、分けるか）、取り込んだ会話の扱い（アプリの会話にするか、読むだけか）。
2. **ファイルと差分のパネル**（Code の右側、スマホではシート）
   - 聞くこと: ツリーの深さ、表示できるファイルの種類、git diff の単位（作業ツリー全体か、ターンごとか）。
3. **端末**（xterm.js と PTY を WebSocket でつなぐ）
   - 聞くこと: 権限（`OMNI_ALLOW_SHELL` との関係）、置き場所。
4. **git worktree での並列実行**
   - 聞くこと: 同じプロジェクトで複数のエージェントを走らせる見せ方、結果の比べ方。
5. **アカウント操作**
   - 何をするか: 状態の表示と、`.env` で許可した Codex のログイン・ログアウトなど。
   - 注意: Claude.ai のログインはアプリに作らない。
6. ChatでのMCPツール、Antigravity CLI（ACP、`agy acp`）

## 実装のときの約束ごと

- 新しいエージェントの承認は、必ず `Run.ask`（→ `policy.decide`）を通す。
- 返答のブロックを変えるときは、`omniws/blocks.py` と `web/src/blocks.ts` を両方変える。
- テストでは本物のCLIを使わない。偽のサーバー（`tests/fake_acp_agent.py`、`tests/fake_codex_app_server.py`）や単体テストで確かめ、本物での確認は最後に最小限だけ行う。
- 常駐版を再起動するのはユーザー（このアプリの上で作業中なら、自分のターンが止まるため）。
