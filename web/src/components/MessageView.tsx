import { useState } from 'react'
import ReactMarkdown from 'react-markdown'
import rehypeHighlight from 'rehype-highlight'
import remarkGfm from 'remark-gfm'
import { Check, Copy, GitBranch } from 'lucide-react'
import { errorText, textOf, type Message, type Provider, type Usage } from '../api'

const STATUS_LABEL: Record<string, string> = { cancelled: '停止しました', interrupted: 'サーバーの再起動で中断しました' }

function formatTokens(value: number): string {
  return value >= 10_000 ? `${(value / 1000).toFixed(value >= 100_000 ? 0 : 1)}k` : value.toLocaleString()
}

export function UsageLine({ usage }: { usage: Usage }) {
  const parts = [`入力 ${formatTokens(usage.inputTokens ?? 0)}`, `出力 ${formatTokens(usage.outputTokens ?? 0)}`]
  if (usage.cacheReadTokens) parts.push(`キャッシュ ${formatTokens(usage.cacheReadTokens)}`)
  if (usage.reasoningTokens) parts.push(`推論 ${formatTokens(usage.reasoningTokens)}`)
  if (usage.costUsd !== undefined) parts.push(`$${usage.costUsd.toFixed(4)}`)
  if (usage.contextWindow) {
    const used = (usage.inputTokens ?? 0) + (usage.cacheReadTokens ?? 0) + (usage.cacheWriteTokens ?? 0) + (usage.outputTokens ?? 0)
    parts.push(`文脈 ${Math.max(1, Math.round((used / usage.contextWindow) * 100))}% / ${formatTokens(usage.contextWindow)}`)
  }
  return <span>{parts.join(' · ')}</span>
}

function CopyButton({ text, label = 'コピー' }: { text: string; label?: string }) {
  const [done, setDone] = useState(false)
  return (
    <button
      onClick={() => navigator.clipboard?.writeText(text).then(() => { setDone(true); setTimeout(() => setDone(false), 1500) })}
      className="rounded p-1 text-muted hover:text-fg"
      title={label}
      aria-label={label}
    >
      {done ? <Check className="size-3.5" aria-hidden /> : <Copy className="size-3.5" aria-hidden />}
    </button>
  )
}

export function Markdown({ text }: { text: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[[rehypeHighlight, { detect: false, ignoreMissing: true }]]}
        components={{
          a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer" />,
          pre: ({ node, children, ...props }) => {
            const code = node?.children?.[0]
            const raw = code && 'children' in code ? code.children.map((child) => ('value' in child ? child.value : '')).join('') : ''
            return (
              <div className="group relative">
                <pre {...props}>{children}</pre>
                <div className="absolute right-1 top-1 opacity-70 group-hover:opacity-100">
                  <CopyButton text={raw} label="コードをコピー" />
                </div>
              </div>
            )
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  )
}

export default function MessageView({ message, providers, streaming, onFork }: {
  message: Message
  providers: Provider[]
  streaming?: { text: string; thought: string; usage: Usage | null }
  onFork: () => void
}) {
  if (message.role === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] whitespace-pre-wrap break-words rounded-2xl bg-raised px-4 py-2.5">{textOf(message)}</div>
      </div>
    )
  }
  const text = streaming ? streaming.text : textOf(message)
  const thought = streaming ? streaming.thought : message.content.filter((b) => b.type === 'thinking').map((b) => b.text).join('')
  const usage = streaming ? streaming.usage : message.usage
  const provider = providers.find((item) => item.id === message.provider)
  const modelName = provider?.models.find((item) => item.id === message.model)?.name ?? message.model
  const live = message.status === 'streaming'

  return (
    <div className="group">
      <div className="mb-1 flex items-center gap-1.5 text-xs text-muted">
        <span className="font-medium">{modelName}</span>
        {provider && <span>· {provider.label}</span>}
        {live && <span className="size-1.5 animate-pulse rounded-full bg-accent" aria-label="応答中" />}
      </div>
      {thought && (
        <details className="mb-2 rounded-lg border border-border bg-surface px-3 py-2 text-sm text-muted" open={live && !text}>
          <summary className="cursor-pointer select-none">思考</summary>
          <div className="mt-2 whitespace-pre-wrap">{thought}</div>
        </details>
      )}
      {text ? <Markdown text={text} /> : live && !thought ? <p className="text-muted">…</p> : null}
      {message.status === 'error' && message.error && (
        <p className="mt-2 rounded-lg border border-danger/40 px-3 py-2 text-sm text-danger">
          {errorText[message.error.code] ?? errorText.unknown}
          {message.error.message && <span className="block text-xs opacity-80">{message.error.message}</span>}
        </p>
      )}
      {STATUS_LABEL[message.status] && <p className="mt-2 text-xs text-warn">{STATUS_LABEL[message.status]}</p>}
      {!live && (
        <div className="mt-1 flex flex-wrap items-center gap-1 text-xs text-muted">
          {usage && <UsageLine usage={usage} />}
          <span className="ml-auto flex md:opacity-0 md:group-hover:opacity-100">
            {text && <CopyButton text={text} />}
            <button onClick={onFork} className="rounded p-1 hover:text-fg" title="ここから分岐" aria-label="ここから分岐">
              <GitBranch className="size-3.5" aria-hidden />
            </button>
          </span>
        </div>
      )}
    </div>
  )
}
