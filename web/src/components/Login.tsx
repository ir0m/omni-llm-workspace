import { useState } from 'react'
import { CircleDot } from 'lucide-react'
import { api, ApiError } from '../api'

export default function Login({ onSignedIn }: { onSignedIn: () => void }) {
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api.login(password)
      onSignedIn()
    } catch (caught) {
      const status = caught instanceof ApiError ? caught.status : 0
      setError(status === 401 ? 'パスワードが違います' : status === 429 ? '試行回数が多すぎます。しばらく待ってください' : 'ログインできませんでした')
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="flex h-full items-center justify-center p-4">
      <form onSubmit={submit} className="w-full max-w-xs rounded-2xl border border-border bg-surface p-6">
        <div className="flex items-center gap-2 text-lg font-semibold">
          <CircleDot className="size-5 text-accent" aria-hidden />
          OmniLLMWorkspace
        </div>
        <label className="mt-6 block text-sm text-muted" htmlFor="password">
          パスワード
        </label>
        <input
          id="password"
          type="password"
          autoComplete="current-password"
          autoFocus
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          className="mt-1 w-full rounded-lg border border-border bg-bg px-3 py-2 outline-none focus:border-accent"
        />
        {error && <p className="mt-2 text-sm text-danger">{error}</p>}
        <button
          type="submit"
          disabled={busy || !password}
          className="mt-4 w-full rounded-lg bg-accent px-3 py-2 font-medium text-accent-fg disabled:opacity-50"
        >
          ログイン
        </button>
      </form>
    </main>
  )
}
