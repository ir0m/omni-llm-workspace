import { useCallback, useEffect, useState } from 'react'
import { api } from './api'
import Login from './components/Login'
import Workspace from './components/Workspace'

type State = 'loading' | 'signed-out' | 'signed-in' | 'no-password' | 'offline'

export default function App() {
  const [state, setState] = useState<State>('loading')

  const check = useCallback(() => {
    api
      .me()
      .then((me) => setState(!me.password_set ? 'no-password' : me.signed_in ? 'signed-in' : 'signed-out'))
      .catch(() => setState('offline'))
  }, [])

  useEffect(check, [check])

  if (state === 'loading') return <Centered>読み込み中…</Centered>
  if (state === 'offline') return <Centered>サーバーに接続できません</Centered>
  if (state === 'no-password')
    return (
      <Centered>
        <p className="font-medium">パスワードが未設定です</p>
        <p className="mt-2 text-sm text-muted">
          .env に <code className="font-mono">OMNI_PASSWORD</code> を書いて、サーバーを再起動してください。
        </p>
      </Centered>
    )
  if (state === 'signed-out') return <Login onSignedIn={() => setState('signed-in')} />
  return <Workspace onSignedOut={() => setState('signed-out')} />
}

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <main className="flex h-full items-center justify-center p-4 text-center">
      <div className="max-w-sm">{children}</div>
    </main>
  )
}
