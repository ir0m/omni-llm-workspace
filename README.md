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

## ライセンス

MIT
