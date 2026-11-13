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
| `OMNI_PROJECTS_DIR` | Code のプロジェクトを置くフォルダ。この直下のフォルダが、1つずつプロジェクトになります。既存のリポジトリはシンボリックリンクで置けます。 |

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
- `OMNI_ALLOW_SHELL`: コマンドの実行を許すか
- `OMNI_ALLOW_NETWORK`: エージェントのネットワーク接続を許すか
- `OMNI_SHELL_DENY`: 常に拒否するコマンド

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
