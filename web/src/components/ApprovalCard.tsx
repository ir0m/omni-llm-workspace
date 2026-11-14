import { useState } from 'react'
import { ShieldAlert } from 'lucide-react'
import type { PermissionAsk } from '../api'
import { DiffView } from './ToolCard'

const ACTION: Record<string, string> = {
  execute: 'コマンドを実行します', edit: 'ファイルを編集します', delete: 'ファイルを削除します', move: 'ファイルを移動します',
  read: 'ファイルを読みます', search: '検索します', fetch: 'Webから取得します', other: '操作をします',
  mcp: 'MCP ツールを使います',
}

// Same rule as the server: chained, piped or redirected commands are never remembered
const SHELL_CONTROL = /[;&|`<>\n]|\$\(/

function alwaysLabel(ask: PermissionAsk): string | null {
  if (ask.kind === 'execute') {
    if (!ask.command || SHELL_CONTROL.test(ask.command)) return null
    return `「${ask.command.trim().split(/\s+/).slice(0, 2).join(' ')}」で始まるコマンドを常に許可`
  }
  if (ask.kind === 'mcp') return 'この会話ではこのツールを常に許可'
  if (ask.kind === 'edit') return ask.outside_project ? 'プロジェクト外の編集も常に許可' : 'この会話では編集を常に許可'
  return 'この会話では常に許可'
}

export default function ApprovalCard({ ask, agent, project, onAnswer }: {
  ask: PermissionAsk
  agent: string
  project: string | null
  onAnswer: (outcome: 'allow' | 'allow_always' | 'deny', reason?: string) => void
}) {
  const [denying, setDenying] = useState(false)
  const [reason, setReason] = useState('')
  const [showDiff, setShowDiff] = useState(true)
  const always = alwaysLabel(ask)

  return (
    <div className="mx-auto mb-2 flex max-h-[55dvh] max-w-3xl flex-col rounded-2xl border border-warn/60 bg-bg p-3 shadow-lg">
      <div className="flex shrink-0 items-center gap-2 text-sm font-medium">
        <ShieldAlert className="size-4 text-warn" aria-hidden />
        承認が必要です · {agent} が{ACTION[ask.kind] ?? ACTION.other}
      </div>
      {/* Long commands and diffs scroll here, so the answer buttons always stay on screen */}
      <div className="min-h-0 shrink overflow-y-auto overscroll-contain">
        {ask.command && (
          <>
            {ask.kind === 'mcp' && <p className="mt-2 break-all font-mono text-sm font-medium">{ask.title}</p>}
            <pre className="mt-2 whitespace-pre-wrap break-all rounded-lg bg-raised px-3 py-2 font-mono text-sm">{ask.kind === 'mcp' ? '' : '$ '}{ask.command}</pre>
          </>
        )}
        {ask.paths.length > 0 && !ask.command && (
          <p className="mt-2 truncate font-mono text-sm">{ask.paths.join(', ')}</p>
        )}
        {ask.diff && (
          <div className="mt-2">
            <button onClick={() => setShowDiff((value) => !value)} className="text-xs text-muted underline">{showDiff ? '差分を隠す' : '差分を見る'}</button>
            {showDiff && <div className="mt-1"><DiffView diff={ask.diff} /></div>}
          </div>
        )}
        <p className={`mt-2 text-xs ${ask.outside_project ? 'text-danger' : 'text-muted'}`}>
          {ask.kind === 'mcp' ? '入力の内容がこのツールに送られます'
            : ask.outside_project ? '⚠ プロジェクトの外を対象にしています' : `場所: ${project ?? '—'}`}
        </p>
      </div>
      {denying ? (
        <div className="mt-3 shrink-0">
          <textarea
            autoFocus
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            rows={2}
            placeholder="理由やかわりにしてほしいこと（任意）"
            className="w-full resize-none rounded-lg border border-border bg-surface px-3 py-2 text-sm outline-none focus:border-accent"
          />
          <div className="mt-2 flex flex-wrap justify-end gap-2 text-sm">
            <button onClick={() => setDenying(false)} className="rounded-lg px-3 py-1.5 text-muted">戻る</button>
            <button onClick={() => onAnswer('deny', reason.trim() || undefined)} className="rounded-lg bg-danger px-3 py-1.5 font-medium text-white">
              {reason.trim() ? '拒否して理由を送る' : '拒否'}
            </button>
          </div>
        </div>
      ) : (
        <div className="mt-3 flex shrink-0 flex-wrap gap-2 text-sm">
          <button onClick={() => onAnswer('allow')} className="rounded-lg bg-accent px-4 py-1.5 font-medium text-accent-fg">許可</button>
          {always ? (
            <button onClick={() => onAnswer('allow_always')} className="rounded-lg border border-border px-3 py-1.5">{always}</button>
          ) : (
            <span className="self-center text-xs text-muted">つないだコマンドは毎回確認します</span>
          )}
          <button onClick={() => setDenying(true)} className="rounded-lg border border-border px-3 py-1.5 text-danger">拒否…</button>
        </div>
      )}
    </div>
  )
}
