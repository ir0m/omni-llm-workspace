import { useCallback, useEffect, useState } from 'react'
import { KeyRound, Trash2, X } from 'lucide-react'
import { api, type PasskeyInfo } from '../api'
import { deviceName, describeError, passkeyProblem, registerPasskey } from '../passkey'

function day(ms: number | null): string {
  return ms ? new Date(ms).toLocaleDateString('ja-JP', { year: 'numeric', month: 'numeric', day: 'numeric' }) : '—'
}

export default function PasskeyDialog({ onClose, onSignedOut }: { onClose: () => void; onSignedOut: () => void }) {
  const [items, setItems] = useState<PasskeyInfo[]>([])
  const [name, setName] = useState(deviceName)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const problem = passkeyProblem(null)

  const load = useCallback(() => {
    api.passkeys().then(setItems).catch((caught) => setError(describeError(caught)))
  }, [])
  useEffect(load, [load])

  async function add() {
    setBusy(true)
    setError(null)
    try {
      await registerPasskey(name.trim() || deviceName())
      load()
    } catch (caught) {
      setError(describeError(caught))
    } finally {
      setBusy(false)
    }
  }

  async function remove(item: PasskeyInfo) {
    const warning = item.current ? '\nこの端末のログインも終了します。' : ''
    if (!window.confirm(`「${item.name}」のパスキーを削除しますか？${warning}`)) return
    try {
      await api.deletePasskey(item.id)
      if (item.current) onSignedOut()
      else load()
    } catch (caught) {
      setError(describeError(caught))
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div onClick={(event) => event.stopPropagation()} className="w-full max-w-md rounded-2xl border border-border bg-bg p-5">
        <div className="flex items-center justify-between">
          <h2 className="flex items-center gap-2 font-semibold"><KeyRound className="size-4" aria-hidden /> パスキー</h2>
          <button onClick={onClose} className="rounded p-1 text-muted" aria-label="閉じる"><X className="size-4" aria-hidden /></button>
        </div>
        <ul className="mt-3 divide-y divide-border text-sm">
          {items.map((item) => (
            <li key={item.id} className="flex items-center gap-2 py-2">
              <div className="min-w-0 flex-1">
                <p className="truncate font-medium">{item.name}{item.current && <span className="ml-2 text-xs text-accent">この端末</span>}</p>
                <p className="truncate text-xs text-muted">{item.rp_id} · 登録 {day(item.created_at)} · 最後に使用 {day(item.last_used_at)}</p>
              </div>
              <button onClick={() => remove(item)} disabled={items.length <= 1} title={items.length <= 1 ? '最後のパスキーは削除できません' : '削除'}
                className="rounded p-1.5 text-muted hover:text-danger disabled:opacity-30" aria-label={`${item.name}を削除`}>
                <Trash2 className="size-4" aria-hidden />
              </button>
            </li>
          ))}
        </ul>
        <div className="mt-4 border-t border-border pt-4">
          <p className="text-sm font-medium">この端末を追加</p>
          <div className="mt-2 flex gap-2">
            <input value={name} onChange={(event) => setName(event.target.value)} aria-label="端末の名前"
              className="min-w-0 flex-1 rounded-lg border border-border bg-surface px-3 py-1.5 text-sm outline-none focus:border-accent" />
            <button onClick={add} disabled={busy || problem !== null}
              className="shrink-0 rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-accent-fg disabled:opacity-50">追加</button>
          </div>
          <p className="mt-2 text-xs text-muted">別の端末は、その端末でこの画面を開いて追加するか、サーバーで passkey-link を実行してリンクから登録します。</p>
          {(problem || error) && <p className="mt-2 text-sm text-danger">{problem ?? error}</p>}
        </div>
      </div>
    </div>
  )
}
