import { useState } from 'react'
import ReactMarkdown from 'react-markdown'
import rehypeHighlight from 'rehype-highlight'
import remarkGfm from 'remark-gfm'
import { Check, Copy, GitBranch } from 'lucide-react'
import { errorText, textOf, type Block, type Executor, type Message, type ToolCall, type Usage } from '../api'
import { PlanSummary } from './PlanBar'
import { isReading, ReadGroup, ToolCard } from './ToolCard'

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
    const used = usage.contextUsed ?? (usage.inputTokens ?? 0) + (usage.cacheReadTokens ?? 0) + (usage.cacheWriteTokens ?? 0) + (usage.outputTokens ?? 0)
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

const AGENT_LABEL: Record<string, string> = { opencode: 'OpenCode', codex: 'Codex', claude_code: 'Claude Code' }

type Piece = { key: string; node: React.ReactNode }

/** Blocks in order; consecutive reads become one line, the plan is shown above the composer while live. */
function pieces(blocks: Block[], live: boolean): Piece[] {
  const out: Piece[] = []
  const last = blocks.length - 1
  for (let index = 0; index < blocks.length; index++) {
    const block = blocks[index]
    if (block.type === 'handover' || (block.type === 'plan' && live)) continue
    if (block.type === 'plan') {
      out.push({ key: `plan-${index}`, node: <PlanSummary entries={block.entries} /> })
    } else if (block.type === 'text') {
      if (block.text) out.push({ key: `text-${index}`, node: <Markdown text={block.text} /> })
    } else if (block.type === 'thinking') {
      if (!block.text) continue
      out.push({
        key: `thinking-${index}`,
        node: (
          <details className="rounded-lg border border-border bg-surface px-3 py-2 text-sm text-muted" open={live && index === last}>
            <summary className="cursor-pointer select-none">思考</summary>
            <div className="mt-2 whitespace-pre-wrap">{block.text}</div>
          </details>
        ),
      })
    } else if (block.type === 'tool_call') {
      const run: ToolCall[] = []
      while (index <= last && blocks[index].type === 'tool_call' && isReading(blocks[index] as ToolCall)) {
        run.push(blocks[index] as ToolCall)
        index++
      }
      if (run.length >= 2) {
        out.push({ key: `reads-${run[0].id}`, node: <ReadGroup blocks={run} /> })
        index--
      } else if (run.length === 1) {
        out.push({ key: run[0].id, node: <ToolCard block={run[0]} /> })
        index--
      } else {
        out.push({ key: block.id, node: <ToolCard block={block} /> })
      }
    }
  }
  return out
}

export default function MessageView({ message, providers, streaming, onFork, onRetryElsewhere }: {
  message: Message
  providers: Executor[]
  streaming?: { blocks: Block[]; usage: Usage | null }
  onFork: () => void
  onRetryElsewhere?: () => void
}) {
  if (message.role === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] whitespace-pre-wrap break-words rounded-2xl bg-raised px-4 py-2.5">{textOf(message)}</div>
      </div>
    )
  }
  const live = message.status === 'streaming'
  const blocks = streaming ? streaming.blocks : message.content
  const usage = streaming ? streaming.usage : message.usage
  const text = textOf({ content: blocks })
  const provider = providers.find((item) => item.id === message.provider)
  const modelName = provider?.models.find((item) => item.id === message.model)?.name ?? message.model
  const handover = blocks.find((block) => block.type === 'handover')
  const parts = pieces(blocks, live)

  return (
    <div className="group">
      {handover && handover.type === 'handover' && (
        <div className="mb-3 flex items-center gap-2 text-xs text-muted">
          <span className="h-px flex-1 bg-border" />
          → {handover.agent} に引き継ぎ（発言{handover.count}件）
          <span className="h-px flex-1 bg-border" />
        </div>
      )}
      <div className="mb-1 flex items-center gap-1.5 text-xs text-muted">
        <span className="font-medium">{modelName}</span>
        {provider && <span>· {provider.label}</span>}
        {message.agent && message.agent !== message.provider && <span>· {AGENT_LABEL[message.agent] ?? message.agent} 経由</span>}
        {live && <span className="size-1.5 animate-pulse rounded-full bg-accent" aria-label="応答中" />}
      </div>
      <div className="flex flex-col gap-2">
        {parts.map((part) => <div key={part.key}>{part.node}</div>)}
        {live && parts.length === 0 && <p className="text-muted">…</p>}
      </div>
      {message.status === 'error' && message.error && (
        <p className="mt-2 rounded-lg border border-danger/40 px-3 py-2 text-sm text-danger">
          {errorText[message.error.code] ?? errorText.unknown}
          {message.error.message && <span className="block text-xs opacity-80">{message.error.message}</span>}
          {message.error.code === 'rate_limit' && onRetryElsewhere && (
            <button onClick={onRetryElsewhere} className="mt-2 rounded-lg border border-danger/40 px-3 py-1 text-xs font-medium hover:bg-danger/10">
              別の実行元で続ける
            </button>
          )}
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
