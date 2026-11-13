import type { Executor, UsageWindow } from '../api'

export const WARN_PERCENT = 90
const STATUS_TEXT: Record<string, string> = { allowed: '余裕あり', allowed_warning: '警告', rejected: '上限' }

/** The percentage, or Claude's status when it sent no percentage. */
function amount(window: UsageWindow): string {
  if (window.usedPercent !== null && window.usedPercent !== undefined) return `${window.usedPercent}%`
  return (window.status && STATUS_TEXT[window.status]) || '—'
}

function effective(window: UsageWindow): number | null {
  if (window.usedPercent !== null && window.usedPercent !== undefined) return window.usedPercent
  return window.status === 'rejected' ? 100 : window.status === 'allowed_warning' ? WARN_PERCENT : null
}

function resetText(resetsAt: number | null): string {
  if (!resetsAt) return ''
  const when = new Date(resetsAt * 1000)
  const soon = when.getTime() - Date.now() < 24 * 3600 * 1000
  const text = soon
    ? when.toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' })
    : when.toLocaleDateString('ja-JP', { month: 'numeric', day: 'numeric' })
  return soon ? `${text}にリセット` : `${text}にリセット`
}

function tone(percent: number | null): string {
  if (percent === null) return 'bg-muted'
  if (percent >= 100) return 'bg-danger'
  if (percent >= WARN_PERCENT) return 'bg-warn'
  return 'bg-accent'
}

/** "5時間16% 週16%" for the executor picker. */
export function usageLabel(executor: Executor): string {
  const windows = executor.usage?.windows ?? []
  if (windows.length === 0) return ''
  const high = windows.some((window) => (effective(window) ?? 0) >= WARN_PERCENT)
  return `${high ? ' ⚠' : ''} ${windows.map((window) => `${window.label}${amount(window)}`).join(' ')}`
}

function Row({ window }: { window: UsageWindow }) {
  const percent = effective(window)
  return (
    <div className="flex items-center gap-2">
      <span className="w-10 shrink-0 text-right">{window.label}</span>
      <span className="h-1.5 w-20 overflow-hidden rounded-full bg-raised">
        <span className={`block h-full ${tone(percent)}`} style={{ width: `${Math.min(100, percent ?? 0)}%` }} />
      </span>
      <span className={percent !== null && percent >= WARN_PERCENT ? 'font-medium text-warn' : ''}>{amount(window)}</span>
      <span>{resetText(window.resetsAt)}</span>
      {window.at && <span>（{new Date(window.at * 1000).toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' })}時点）</span>}
    </div>
  )
}

/** Below the composer: the selected subscription's usage windows. */
export default function UsageMeter({ executor }: { executor: Executor | undefined }) {
  const windows = executor?.usage?.windows ?? []
  if (!executor || windows.length === 0) return null
  return (
    <div className="mx-auto mt-1.5 flex max-w-3xl flex-wrap items-start gap-x-4 gap-y-0.5 px-2 text-[11px] text-muted">
      <span className="font-medium">{executor.label}</span>
      <div className="space-y-0.5">{windows.map((window) => <Row key={window.label} window={window} />)}</div>
    </div>
  )
}
