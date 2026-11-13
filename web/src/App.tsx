import { useEffect, useState } from 'react'
import { CircleDot } from 'lucide-react'

type Health = { status: string; version: string }

export default function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    fetch('/api/health')
      .then((response) => (response.ok ? response.json() : Promise.reject(response.status)))
      .then(setHealth)
      .catch(() => setFailed(true))
  }, [])

  return (
    <main className="flex h-full items-center justify-center p-4">
      <div className="w-full max-w-sm rounded-2xl border border-border bg-surface p-6">
        <div className="flex items-center gap-2 text-lg font-semibold">
          <CircleDot className="size-5 text-accent" aria-hidden />
          OmniLLMWorkspace
        </div>
        <p className="mt-2 text-sm text-muted">
          API・サブスクリプション・ローカルLLMを切り替えて使う Chat と Code のワークスペース
        </p>
        <p className="mt-4 text-sm">
          {failed ? (
            <span className="text-danger">サーバーに接続できません</span>
          ) : health ? (
            <span className="text-ok">サーバー稼働中（v{health.version}）</span>
          ) : (
            <span className="text-muted">確認中…</span>
          )}
        </p>
      </div>
    </main>
  )
}
