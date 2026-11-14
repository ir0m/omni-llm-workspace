import { lazy, Suspense, useCallback, useEffect, useState } from 'react'
import { api, type Me } from './api'
import Login, { Enroll } from './components/Login'
import Workspace from './components/Workspace'
import { audienceSpec } from './components/presentLink'

const Audience = lazy(() => import('./components/Presenter').then((module) => ({ default: module.Audience })))

function enrollToken(): string | null {
  const match = window.location.hash.match(/^#\/enroll\/([\w-]+)$/)
  return match ? match[1] : null
}

export default function App() {
  const [me, setMe] = useState<Me | null>(null)
  const [offline, setOffline] = useState(false)
  const [token, setToken] = useState<string | null>(enrollToken)

  const check = useCallback(() => {
    api.me().then((value) => {
      setMe(value)
      setOffline(false)
    }).catch(() => setOffline(true))
  }, [])

  useEffect(check, [check])
  useEffect(() => {
    const onHash = () => setToken(enrollToken())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  if (offline) return <Centered>
    <p role="alert">サーバーに接続できません</p>
    <button onClick={() => { setOffline(false); check() }} className="mt-4 rounded-lg bg-accent px-4 py-2 text-accent-fg">再接続</button>
  </Centered>
  if (!me) return <Centered>読み込み中…</Centered>
  if (token && !me.signed_in) return <Enroll token={token} me={me} onDone={() => { setToken(null); check() }} />
  if (!me.signed_in) return <Login me={me} onSignedIn={check} />
  // The projector's window shows only the slides
  const audience = audienceSpec()
  if (audience) return <Suspense fallback={null}><Audience spec={audience} /></Suspense>
  return <Workspace onSignedOut={() => { window.location.hash = ''; check() }} />
}

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <main className="flex h-full items-center justify-center p-4 text-center">
      <div className="max-w-sm">{children}</div>
    </main>
  )
}
