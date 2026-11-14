import { useRef, useState } from 'react'
import { api, type ConversationDetail, type Target } from '../api'

export default function ContextPanel({ detail, target, onChanged, onError }: {
  detail: ConversationDetail; target: Target | null; onChanged: () => void; onError: (error: unknown) => void
}) {
  const lock = useRef(false)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  async function act(kind: 'summary' | 'native' | 'clear') {
    if (lock.current || !target) return
    lock.current = true
    setBusy(true)
    setNotice('')
    try {
      if (kind === 'clear') await api.clearContext(detail.id)
      else if (kind === 'native') await api.compactNativeContext(detail.id, target)
      else await api.compactContext(detail.id, target)
      setNotice(kind === 'clear' ? '全文を使う設定に戻しました' : kind === 'native' ? '実行元のコンテキストを圧縮しました' : '引き継ぎ要約を更新しました')
      onChanged()
    } catch (error) { onError(error) }
    finally { lock.current = false; setBusy(false) }
  }
  return <details className="mx-auto mb-2 max-w-3xl rounded-lg border border-border p-2 text-xs">
    <summary className="cursor-pointer">コンテキスト{detail.context ? ' · 引き継ぎ要約あり' : ''}</summary>
    <p className="my-2 text-muted">共通要約は、選択中の実行元で古い会話を要約し、直近3往復を残します。生成には利用量がかかります。元の履歴は保存されます。</p>
    <div className="flex flex-wrap gap-2">
      <button disabled={busy || !target} onClick={() => act('summary')} className="rounded border border-border px-2 py-1 disabled:opacity-40">{busy ? '処理中…' : '共通要約を作成・更新'}</button>
      <button disabled={busy || !target} onClick={() => act('native')} className="rounded border border-border px-2 py-1 disabled:opacity-40">実行元の圧縮</button>
      {detail.context && <button disabled={busy} onClick={() => act('clear')} className="rounded border border-border px-2 py-1 disabled:opacity-40">共通要約を解除</button>}
    </div>
    <p className="mt-2 text-muted">実行元の圧縮はClaude API、Responses方式のOpenAI API、Codex・Claude Codeに対応します。CLIの既存セッションにはこちらを使います。</p>
    {notice && <p role="status" className="mt-2">{notice}</p>}
    {detail.context && <>
      <p className="mt-2 text-muted">発言#{detail.context.through_seq}まで · {detail.context.source_chars.toLocaleString()}文字 → {detail.context.text.length.toLocaleString()}文字 · 要約の入力{detail.context.usage.inputTokens.toLocaleString()} / 出力{detail.context.usage.outputTokens.toLocaleString()}トークン</p>
      <pre className="mt-2 max-h-48 overflow-y-auto whitespace-pre-wrap break-words">{detail.context.text}</pre>
    </>}
  </details>
}
