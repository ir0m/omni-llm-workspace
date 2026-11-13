import { useCallback, useEffect, useState } from 'react'
import { api, type Me } from './api'
import Login, { Enroll } from './components/Login'
import Workspace from './components/Workspace'

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

  if (offline) return <Centered>サーバーに接続できません</Centered>
  if (!me) return <Centered>読み込み中…</Centered>
  if (token && !me.signed_in) return <Enroll token={token} me={me} onDone={() => { setToken(null); check() }} />
  if (!me.signed_in) return <Login me={me} onSignedIn={check} />
  return <Workspace onSignedOut={() => { window.location.hash = ''; check() }} />
}

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <main className="flex h-full items-center justify-center p-4 text-center">
      <div className="max-w-sm">{children}</div>
    </main>
  )
}
