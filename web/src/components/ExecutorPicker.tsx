import { RefreshCw } from 'lucide-react'
import type { Executor, Mode, Target } from '../api'
import { usageLabel } from './UsageMeter'

const KIND_ORDER = ['subscription', 'api', 'local'] as const
const KIND_LABEL: Record<string, string> = { subscription: 'サブスク', api: 'API', local: 'ローカル' }
const EFFORT_LABEL: Record<string, string> = { low: '低', medium: '中', high: '高', xhigh: '最高', max: '最大' }

export function findModel(executors: Executor[], target: Target | null) {
  if (!target) return undefined
  return executors.find((item) => item.id === target.provider)?.models.find((model) => model.id === target.model)
}

/** Whether an executor can take a turn in this mode now. */
export function usable(executor: Executor, mode: Mode): boolean {
  return executor.configured && executor.models.length > 0 && executor.modes.includes(mode)
}

export function firstTarget(executors: Executor[], mode: Mode = 'chat'): Target | null {
  const executor = executors.find((item) => usable(item, mode))
  return executor ? { provider: executor.id, model: executor.models[0].id, effort: null } : null
}

const select = 'shrink-0 truncate rounded-lg border border-border bg-bg px-2 py-1.5 text-sm'

export default function ExecutorPicker({ executors, mode, target, onChange, onRefresh }: {
  executors: Executor[]
  mode: Mode
  target: Target | null
  onChange: (target: Target) => void
  onRefresh: () => void
}) {
  const executor = executors.find((item) => item.id === target?.provider)
  const model = findModel(executors, target)

  function chooseExecutor(id: string) {
    const next = executors.find((item) => item.id === id)
    if (next) onChange({ provider: next.id, model: next.models[0]?.id ?? '', effort: null })
  }

  return (
    <div className="flex shrink-0 items-center gap-1.5">
      <select value={executor?.id ?? ''} onChange={(event) => chooseExecutor(event.target.value)} className={`${select} max-w-40`} aria-label="実行元">
        {!executor && <option value="">実行元</option>}
        {KIND_ORDER.map((kind) => {
          const items = executors.filter((item) => item.kind === kind)
          if (items.length === 0) return null
          return (
            <optgroup key={kind} label={KIND_LABEL[kind]}>
              {items.map((item) => {
                const reason = !item.configured ? `未設定: ${item.setting}`
                  : item.models.length === 0 ? (item.error ? '取得できません' : 'モデルなし')
                  : !item.modes.includes(mode) ? (mode === 'code' && item.code_via ? 'Codeは次の段階で対応' : `${mode === 'code' ? 'Code' : 'Chat'}非対応`)
                  : null
                return (
                  <option key={item.id} value={item.id} disabled={reason !== null}>
                    {item.label}{reason ? `（${reason}）` : ''}{item.note ? ` ${item.note}` : ''}{reason ? '' : usageLabel(item)}
                  </option>
                )
              })}
            </optgroup>
          )
        })}
      </select>
      {executor && (
        <select
          value={target?.model ?? ''}
          onChange={(event) => target && onChange({ ...target, model: event.target.value, effort: null })}
          className={`${select} max-w-48 sm:max-w-64`}
          aria-label="モデル"
        >
          {!model && target?.model && <option value={target.model}>{target.model}</option>}
          {executor.models.map((item) => (
            <option key={item.id} value={item.id}>{item.name === item.id ? item.id : `${item.name}`}</option>
          ))}
        </select>
      )}
      {model && model.efforts.length > 0 && target && (
        <select
          value={target.effort ?? ''}
          onChange={(event) => onChange({ ...target, effort: event.target.value || null })}
          className={select}
          aria-label="思考の深さ"
          title="思考の深さ（effort）"
        >
          <option value="">深さ: 既定</option>
          {model.efforts.map((effort) => (
            <option key={effort} value={effort}>深さ: {EFFORT_LABEL[effort] ?? effort}</option>
          ))}
        </select>
      )}
      <button onClick={onRefresh} className="shrink-0 rounded-lg p-1.5 text-muted hover:text-fg" title="一覧を更新" aria-label="実行元とモデルの一覧を更新">
        <RefreshCw className="size-4" aria-hidden />
      </button>
    </div>
  )
}
