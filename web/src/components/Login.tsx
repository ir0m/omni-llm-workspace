import { useState } from 'react'
import { CircleDot, KeyRound } from 'lucide-react'
import { api, ApiError, type Me } from '../api'
import { deviceName, describeError, passkeyProblem, registerPasskey, signInWithPasskey } from '../passkey'

function Card({ children }: { children: React.ReactNode }) {
  return (
    <main className="flex h-full items-center justify-center p-4">
      <div className="w-full max-w-sm rounded-2xl border border-border bg-surface p-6">
        <div className="flex items-center gap-2 text-lg font-semibold">
          <CircleDot className="size-5 text-accent" aria-hidden />
          OmniLLMWorkspace
        </div>
        {children}
      </div>
    </main>
  )
}

/** Opened from the one-time link made by `python -m omniws passkey-link`. */
export function Enroll({ token, me, onDone }: { token: string; me: Me | null; onDone: () => void }) {
  const [name, setName] = useState(deviceName)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const problem = passkeyProblem(me)

  async function enroll() {
    setBusy(true)
    setError(null)
    try {
      await registerPasskey(name.trim() || deviceName(), token)
      window.location.hash = ''
      onDone()
    } catch (caught) {
      setError(describeError(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card>
      <p className="mt-4 text-sm">この端末をパスキーで登録します。以後は Face ID や指紋などでログインできます。</p>
      <label className="mt-4 block text-sm text-muted" htmlFor="device">端末の名前</label>
      <input id="device" value={name} onChange={(event) => setName(event.target.value)}
        className="mt-1 w-full rounded-lg border border-border bg-bg px-3 py-2 outline-none focus:border-accent" />
      {(problem || error) && <p className="mt-2 text-sm text-danger">{problem ?? error}</p>}
      <button onClick={enroll} disabled={busy || problem !== null}
        className="mt-4 flex w-full items-center justify-center gap-2 rounded-lg bg-accent px-3 py-2 font-medium text-accent-fg disabled:opacity-50">
        <KeyRound className="size-4" aria-hidden /> この端末を登録
      </button>
    </Card>
  )
}

export default function Login({ me, onSignedIn }: { me: Me; onSignedIn: () => void }) {
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const problem = passkeyProblem(me)

  async function withPasskey() {
    setBusy(true)
    setError(null)
    try {
      await signInWithPasskey()
      onSignedIn()
    } catch (caught) {
      setError(describeError(caught))
    } finally {
      setBusy(false)
    }
  }

  async function withPassword(event: React.FormEvent) {
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
    <Card>
      {me.registered ? (
        <>
          <button onClick={withPasskey} disabled={busy || problem !== null}
            className="mt-6 flex w-full items-center justify-center gap-2 rounded-lg bg-accent px-3 py-2.5 font-medium text-accent-fg disabled:opacity-50">
            <KeyRound className="size-4" aria-hidden /> パスキーでログイン
          </button>
          {problem && <p className="mt-2 text-sm text-warn">{problem}</p>}
        </>
      ) : (
        <div className="mt-4 text-sm">
          <p className="font-medium">最初の端末を登録してください</p>
          <p className="mt-2 text-muted">サーバーで次を実行し、表示されたリンクをこの端末で開きます（15分・1回限り）。</p>
          <pre className="mt-2 overflow-x-auto rounded-lg bg-raised px-3 py-2 font-mono text-xs">uv run python -m omniws passkey-link</pre>
        </div>
      )}
      {me.password && (
        <form onSubmit={withPassword} className="mt-6 border-t border-border pt-4">
          <label className="block text-xs text-muted" htmlFor="password">パスワード（開発用）</label>
          <input id="password" type="password" autoComplete="current-password" value={password}
            onChange={(event) => setPassword(event.target.value)}
            className="mt-1 w-full rounded-lg border border-border bg-bg px-3 py-2 outline-none focus:border-accent" />
          <button type="submit" disabled={busy || !password}
            className="mt-2 w-full rounded-lg border border-border px-3 py-2 text-sm disabled:opacity-50">パスワードでログイン</button>
        </form>
      )}
      {error && <p className="mt-3 text-sm text-danger">{error}</p>}
    </Card>
  )
}
