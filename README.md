# OmniLLMWorkspace

API・サブスクリプション・ローカルLLMのモデルを自由に切り替えて、プロジェクトに依存しない Chat と、プロジェクトを指定する Code を一つの画面で使うセルフホスト型のワークスペースです。自宅サーバーで動かし、ブラウザから使います。

> 開発中です。使い方は完成時にまとめます。

## 開発

```sh
cp .env.example .env   # 値を書く
uv sync
(cd web && npm ci && npm run build)
uv run python -m omniws  # http://127.0.0.1:8300
```

フロントを開発するときは `cd web && npm run dev`（/api は 8300 に転送）。

## 最初のパスキー登録

`.env` の `OMNI_PUBLIC_URLS` に実際に開くHTTPSのURLを設定して起動します。最初の端末はサーバーで次を実行し、表示されたリンクをその端末で開いて登録します。リンクは15分・1回限り有効です。

```sh
uv run python -m omniws passkey-link
```

以後は「パスキーでログイン」を使います。追加・削除はサイドバー下の鍵アイコンから行えます。全端末を失くした場合も、サーバーで登録リンクを作り直せます。

常駐サービスの設定例は [deploy/systemd/omni-llm-workspace.service](deploy/systemd/omni-llm-workspace.service)、現在の環境と引き継ぎは [docs/handoff.md](docs/handoff.md) を参照してください。

## ドキュメント

- [ロードマップ](docs/roadmap.md)
- [競合・OSS調査とアーキテクチャ再評価（2026-11-13）](docs/research/2026-11-13-landscape.md)

## ライセンス

MIT
