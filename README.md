# OmniLLMWorkspace

自分のサーバーで動かす、LLMのワークスペースです。API・サブスクリプション・ローカルLLMを1つの画面で切り替えて使えます。

- **Chat**: プロジェクトに関係なく、モデルと会話します。
- **Code**: プロジェクトのフォルダで、エージェント（Codex、Claude Code、OpenCode）に作業させます。
- ChatとCodeは、同じ会話の中で切り替えられます。
- スマホのブラウザからも使えます。ログインはパスキーです。

利用者が1人のセルフホストを前提にしています。

## できること

- **実行元の切り替え**: Anthropic・OpenAI・Gemini・OpenRouter のAPI、Ollama などのローカルLLM、Codex（ChatGPTのサブスクリプション）、Claude Code（サブスクリプションまたはAPI）。
- **承認**: エージェントがコマンドの実行やファイルの編集をする前に、画面で確認できます。どこまで自動にするかは、会話ごとに選べます。
- **CLIの会話の取り込み**: ターミナルで使った Codex・Claude Code の会話を一覧に表示し、その続きをアプリから進められます。
- **ファイルと差分**: Code の画面で、プロジェクトのファイルと、コミットしていない変更を見られます。
- **端末**: Code の右パネルから対話型シェルを使えます。スマホでは全画面で開きます。専用の許可が必要です。
- **並列作業**: git worktree ごとに会話を作り、別々の指示、または同じ指示を複数の実行元へ送れます。返答と開始地点からの差分を比較できます。
- **使用率**: サブスクリプションの使用率と、会話の文脈の使用量を表示します。

## 必要なもの

- Linux または macOS
- Python 3.12 以降と [uv](https://docs.astral.sh/uv/)
- Node.js 22 以降（画面のビルドに使います）
- 使いたいものだけ:
  - Code で使うエージェントのCLI: [Codex](https://github.com/openai/codex)、[OpenCode](https://opencode.ai/)。Claude Code は同梱のSDKで動きます。
  - APIキー、またはローカルLLMのサーバー

## インストール

```sh
git clone https://github.com/ir0m/omni-llm-workspace.git
cd omni-llm-workspace
cp .env.example .env
uv sync
(cd web && npm ci && npm run build)
```

## 設定

`.env` に値を書きます。各項目の説明は [.env.example](.env.example) にあります。最低限、次の3つを設定してください。

| 項目 | 内容 |
|---|---|
| `OMNI_SECRET` | ログイン情報の署名に使う長いランダム文字列。`openssl rand -hex 32` などで作ります。 |
| `OMNI_PUBLIC_URLS` | ブラウザで開くURL。パスキーは HTTPS か `http://localhost` でだけ使えます。 |
| `OMNI_PROJECTS_DIR` | Code のプロジェクトを置くフォルダ。この直下のフォルダが、1つずつプロジェクトになります。既存のリポジトリはシンボリックリンクで置けます。画面から作るときに `仕事/顧客A/app` のように `/` で区切ると、5階層までフォルダの中に作れます（途中のフォルダはグループになり、プロジェクトとしては選べません）。 |

使うサービスに合わせて、APIキーやローカルLLMのURLも書きます。

### サブスクリプション

資格情報は `.env` に書きません。各CLIに自分でログインすると、アプリはそのログインを使います。

- ChatGPT（Codex）: `codex login`
- Claude（Claude Code）: `claude auth login` を実行し、`.env` の `OMNI_ALLOW_CLAUDE_SUBSCRIPTION=true` で有効にします。アプリは Claude.ai の資格情報を扱わず、CLI自身のログインだけを使います。利用は個人の責任で行ってください。

### 権限

Code でエージェントができることの上限は `.env` で決まります。画面ではこれより厳しくできますが、緩めることはできません。

- `OMNI_APPROVAL_DEFAULT` / `OMNI_APPROVAL_MAX`: 承認モードの既定値と、選べる一番緩いモード
  - `ask`: 読み取り以外はすべて確認
  - `auto_edit`: プロジェクト内の編集は自動、コマンドは確認
  - `auto`: 拒否するコマンド以外は自動
- `OMNI_OUTSIDE_READ` / `OMNI_OUTSIDE_WRITE`: プロジェクト外の読み取り／編集・コマンド（`mode`=承認モードに従う、`ask`=常に確認、`deny`=常に拒否）
- `OMNI_ALLOW_SHELL`: コマンドの実行を許すか
- `OMNI_ALLOW_NETWORK`: エージェントのネットワーク接続を許すか
- `OMNI_SHELL_DENY`: エージェントに常に拒否するコマンド
- `OMNI_ALLOW_TERMINAL`: 手動の対話型端末を許すか。既定は `false`。有効にすると、サーバーの実行ユーザーとして直接操作できます。エージェントの承認・コマンド拒否・ディレクトリ・ネットワーク制限とは独立した許可です。
- `OMNI_ALLOW_NATIVE_SESSION_CHANGES`: CLI側の改名・分岐・明示的な削除を許すか。既定は `true`。

`.env` などの秘密情報、鍵ファイル、`.git` の中、アプリのデータは、`auto` 以外では常に確認します（「常に許可」や `auto_edit` でも自動にはなりません）。Claude Code には、`.env` と鍵ファイル、アプリのデータの読み取りを許可しません。

### 端末・並列作業・CLIの会話

Code のフォルダボタンから「端末」「並列作業」を開きます。端末は接続ボタンで起動し、タブの切り替え・画面を閉じる操作・通信切断で終了します。リバースプロキシでは WebSocket の転送を有効にしてください。

並列作業は、開始地点をローカル／リモート追跡ブランチの一覧から選び、その先頭コミットから独立したフォルダとブランチを作ります。一覧はサーバー内のGit情報を使い、自動でfetchは行いません。未コミット変更・未追跡ファイル（`.env` など）はコピーしません。実行中の承認は各会話を開いて行います。「元フォルダへ統合」は、両方のフォルダに未コミット変更がなく、fast-forwardできる場合に使えます。ブランチが分岐している場合は端末で統合してください。作業フォルダを削除しても会話とブランチは残ります。

CLIと紐付いた会話の改名・分岐は、元のCLI側にも反映します。削除は「アプリ内のみ削除」「CLI側も削除」から選びます。後者は元に戻せず、Codexが生成した子セッションも対象になります。アプリ内のみ削除した会話は自動で再取り込みしません。

## 起動

```sh
uv run python -m omniws
```

既定では `127.0.0.1:8300` で待ち受けます。外部には直接公開せず、[Tailscale Serve](https://tailscale.com/kb/1312/serve) などのHTTPSのリバースプロキシを使って開いてください。そのURLを `OMNI_PUBLIC_URLS` に書きます。ホスト名が違う場合は、`OMNI_ALLOWED_HOSTS` にも追加します。

### 最初のパスキー登録

サーバーで次を実行すると、登録用のリンクが表示されます。リンクは15分間、1回だけ使えます。

```sh
uv run python -m omniws passkey-link
```

登録したい端末でこのリンクを開き、パスキーを作ります。以後は「パスキーでログイン」を使います。

- パスキーの追加・削除は、サイドバーの下にある鍵のアイコンから行えます。
- すべての端末を失くしたときは、サーバーで登録リンクを作り直します。

### 常駐させる

systemd のユーザーサービスの例が [deploy/systemd/omni-llm-workspace.service](deploy/systemd/omni-llm-workspace.service) にあります。`WorkingDirectory` と `ExecStart` を、クローンした場所に合わせて書き換えてから使ってください。

```sh
cp deploy/systemd/omni-llm-workspace.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now omni-llm-workspace
```

## 更新

```sh
git pull
uv sync
(cd web && npm ci && npm run build)
systemctl --user restart omni-llm-workspace   # 常駐させている場合
```

画面を更新してサーバーをまだ再起動していないときは、サイドバーに「反映待ち」と表示されます。

## 開発

```sh
uv run pytest -q          # バックエンドのテスト
(cd web && npm run dev)   # 画面の開発サーバー（/api は 8300 に転送）
```

## ライセンス

MIT
