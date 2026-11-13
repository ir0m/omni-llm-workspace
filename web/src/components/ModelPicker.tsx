import { RefreshCw } from 'lucide-react'
import type { Provider, Target } from '../api'

const KIND_LABEL: Record<string, string> = { api: 'API', local: 'ローカル', subscription: 'サブスク' }
const EFFORT_LABEL: Record<string, string> = { low: '低', medium: '中', high: '高', xhigh: '最高', max: '最大' }

export function findModel(providers: Provider[], target: Target | null) {
  if (!target) return undefined
  return providers.find((provider) => provider.id === target.provider)?.models.find((model) => model.id === target.model)
}

export function firstTarget(providers: Provider[]): Target | null {
  const provider = providers.find((item) => item.configured && item.models.length > 0)
  return provider ? { provider: provider.id, model: provider.models[0].id, effort: null } : null
}

export default function ModelPicker({ providers, target, onChange, onRefresh }: {
  providers: Provider[]
  target: Target | null
  onChange: (target: Target) => void
  onRefresh: () => void
}) {
  const value = target ? `${target.provider}::${target.model}` : ''
  const model = findModel(providers, target)
  const missing = target && !model  // a model the provider no longer lists (or not loaded yet)

  return (
    <div className="flex min-w-0 items-center gap-1.5">
      <select
        value={value}
        onChange={(event) => {
          const [provider, ...rest] = event.target.value.split('::')
          onChange({ provider, model: rest.join('::'), effort: null })
        }}
        className="min-w-0 max-w-56 truncate rounded-lg border border-border bg-bg px-2 py-1.5 text-sm sm:max-w-80"
        aria-label="モデル"
      >
        {!target && <option value="">モデルを選択</option>}
        {missing && <option value={value}>{target.model}</option>}
        {providers.map((provider) => (
          <optgroup key={provider.id} label={`${provider.label}（${KIND_LABEL[provider.kind] ?? provider.kind}）`}>
            {!provider.configured && (
              <option disabled value="">未設定: .env の {provider.setting}</option>
            )}
            {provider.configured && provider.error && <option disabled value="">取得できません: {provider.error.message || provider.error.code}</option>}
            {provider.models.map((item) => (
              <option key={item.id} value={`${provider.id}::${item.id}`}>
                {item.name === item.id ? item.id : `${item.name} (${item.id})`}
              </option>
            ))}
          </optgroup>
        ))}
      </select>
      {model && model.efforts.length > 0 && target && (
        <select
          value={target.effort ?? ''}
          onChange={(event) => onChange({ ...target, effort: event.target.value || null })}
          className="rounded-lg border border-border bg-bg px-2 py-1.5 text-sm"
          aria-label="思考の深さ"
          title="思考の深さ（effort）"
        >
          <option value="">既定</option>
          {model.efforts.map((effort) => (
            <option key={effort} value={effort}>{EFFORT_LABEL[effort] ?? effort}</option>
          ))}
        </select>
      )}
      <button onClick={onRefresh} className="rounded-lg p-1.5 text-muted hover:text-fg" title="モデル一覧を更新" aria-label="モデル一覧を更新">
        <RefreshCw className="size-4" aria-hidden />
      </button>
    </div>
  )
}
