import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import rehypeHighlight from 'rehype-highlight'
import remarkGfm from 'remark-gfm'
import { mayHaveMath, prepareMath } from '../math'
import { copyText, nodeText } from '../clipboard'
import { Check, Copy, Download, File as FileIcon, GitBranch, X } from 'lucide-react'
import { attachmentsOf, attachmentUrl, errorText, textOf, type AttachmentInfo, type Block, type Executor, type Message, type ToolCall, type Usage } from '../api'
import { PlanSummary } from './PlanBar'
import { sizeLabel } from '../attach'
import { isReading, ReadGroup, ToolCard } from './ToolCard'

const STATUS_LABEL: Record<string, string> = { cancelled: '停止しました', interrupted: 'サーバーの再起動で中断しました' }

function formatTokens(value: number): string {
  return value >= 10_000 ? `${(value / 1000).toFixed(value >= 100_000 ? 0 : 1)}k` : value.toLocaleString()
}

export function UsageLine({ usage }: { usage: Usage }) {
  const [open, setOpen] = useState(false)
  const parts = [`入力 ${formatTokens(usage.inputTokens ?? 0)}`, `出力 ${formatTokens(usage.outputTokens ?? 0)}`]
  if (usage.cacheReadTokens) parts.push(`キャッシュ ${formatTokens(usage.cacheReadTokens)}`)
  if (usage.reasoningTokens) parts.push(`推論 ${formatTokens(usage.reasoningTokens)}`)
  if (usage.costUsd !== undefined) parts.push(`$${usage.costUsd.toFixed(4)}`)
  let context = ''
  if (usage.contextWindow) {
    const used = usage.contextUsed ?? (usage.inputTokens ?? 0) + (usage.cacheReadTokens ?? 0) + (usage.cacheWriteTokens ?? 0) + (usage.outputTokens ?? 0)
    context = `文脈 ${Math.max(1, Math.round((used / usage.contextWindow) * 100))}% / ${formatTokens(usage.contextWindow)}`
  }
  const breakdown = usage.contextBreakdown ?? []
  return (
    <span className="relative">
      {parts.join(' · ')}
      {context && ' · '}
      {context && (breakdown.length > 0 ? (
        <button onClick={() => setOpen((value) => !value)} className="underline decoration-dotted underline-offset-2 hover:text-fg" title="内訳を見る">
          {context}
        </button>
      ) : context)}
      {open && breakdown.length > 0 && (
        <span className="absolute bottom-5 left-0 z-20 block w-64 rounded-lg border border-border bg-bg p-2 text-xs shadow-lg">
          <span className="mb-1 block font-medium text-fg">文脈の内訳</span>
          {breakdown.map((item) => (
            <span key={item.name} className="flex justify-between gap-2">
              <span className="truncate">{item.name}</span>
              <span className="shrink-0 tabular-nums">{formatTokens(item.tokens)}</span>
            </span>
          ))}
        </span>
      )}
    </span>
  )
}

function CopyButton({ text, label = 'コピー' }: { text: string; label?: string }) {
  const [state, setState] = useState<'idle' | 'done' | 'failed'>('idle')
  const timer = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(timer.current), [])
  async function copy() {
    const ok = await copyText(text)
    setState(ok ? 'done' : 'failed')
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setState('idle'), ok ? 1500 : 3000)
  }
  const title = state === 'failed' ? 'コピーできませんでした' : state === 'done' ? 'コピーしました' : label
  return (
    <button onClick={() => void copy()} className={`rounded p-1 hover:text-fg ${state === 'failed' ? 'text-danger' : 'text-muted'}`}
      title={title} aria-label={title}>
      {state === 'done' ? <Check className="size-3.5" aria-hidden /> : state === 'failed' ? <X className="size-3.5" aria-hidden /> : <Copy className="size-3.5" aria-hidden />}
    </button>
  )
}

type MathPlugins = typeof import('../mathPlugins')
let mathPlugins: MathPlugins | null = null
let mathLoading: Promise<MathPlugins> | null = null

/** KaTeX is loaded the first time a message contains math. */
function useMath(wanted: boolean): MathPlugins | null {
  const [plugins, setPlugins] = useState(mathPlugins)
  useEffect(() => {
    if (!wanted || plugins) return
    let active = true
    mathLoading ??= import('../mathPlugins').then((loaded) => (mathPlugins = loaded))
    mathLoading.then((loaded) => { if (active) setPlugins(loaded) }, () => { mathLoading = null })
    return () => { active = false }
  }, [wanted, plugins])
  return wanted ? plugins : null
}

export function Markdown({ text, image }: { text: string; image?: (src: string) => string | null }) {
  const math = useMath(mayHaveMath(text))
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={math ? [remarkGfm, math.remarkMath] : [remarkGfm]}
        rehypePlugins={math
          ? [[math.rehypeKatex, { strict: false, trust: false, throwOnError: false }], [rehypeHighlight, { detect: false, ignoreMissing: true }]]
          : [[rehypeHighlight, { detect: false, ignoreMissing: true }]]}
        components={{
          a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer" />,
          // Documents refer to images beside them; the caller maps those paths to URLs the server can serve.
          ...(image ? { img: ({ node: _node, src, ...props }) => {
            const url = typeof src === 'string' ? image(src) : null
            return url ? <img {...props} src={url} loading="lazy" /> : <span className="text-muted">［画像: {props.alt || String(src ?? '')}］</span>
          } } : {}),
          pre: ({ node, children, ...props }) => {
            // Highlighted code is split into spans: take the text of every descendant, not only direct text nodes
            const raw = nodeText(node?.children?.[0])
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
        {math ? prepareMath(text) : text}
      </ReactMarkdown>
    </div>
  )
}

const AGENT_LABEL: Record<string, string> = {
  opencode: 'OpenCode', codex: 'Codex', claude_code: 'Claude Code', claude_code_api: 'Claude Code', agy: 'Antigravity',
}

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
    } else if (block.type === 'user_input') {
      out.push({ key: `input-${index}`, node: (
        <div className="flex flex-col items-end">
          <div className="max-w-[85%] whitespace-pre-wrap break-words rounded-2xl bg-raised px-4 py-2.5">
            <span className="mb-1 block text-xs text-muted">作業中の指示</span>{block.text}
          </div>
          <CopyButton text={block.text} />
        </div>
      ) })
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

const KIND_LABEL: Record<AttachmentInfo['kind'], string> = { image: '画像', pdf: 'PDF', text: 'テキスト', file: 'ファイル' }

/** Files sent with a message: photos as thumbnails that open in full, everything else as a download. */
function Attachments({ files }: { files: AttachmentInfo[] }) {
  return <ul className="mb-1.5 flex max-w-[85%] flex-wrap justify-end gap-2" aria-label="添付ファイル">
    {files.map((file) => <li key={file.id}>
      {file.kind === 'image'
        ? <a href={attachmentUrl(file.id)} target="_blank" rel="noopener noreferrer" title={file.name}
            className="block overflow-hidden rounded-xl border border-border bg-surface">
            <img src={attachmentUrl(file.id)} alt={file.name} loading="lazy" className="max-h-48 max-w-[16rem] object-contain" />
          </a>
        : <a href={attachmentUrl(file.id, true)} download={file.name} title={`${file.name} をダウンロード`}
            className="flex max-w-[16rem] items-center gap-2 rounded-xl border border-border bg-surface px-2.5 py-2 text-sm hover:bg-raised">
            <FileIcon className="size-4 shrink-0 text-muted" aria-hidden />
            <span className="flex min-w-0 flex-col">
              <span className="truncate">{file.name}</span>
              <span className="text-xs text-muted">{KIND_LABEL[file.kind]} · {sizeLabel(file.size)}</span>
            </span>
            <Download className="size-3.5 shrink-0 text-muted" aria-hidden />
          </a>}
    </li>)}
  </ul>
}

export default function MessageView({ message, providers, streaming, onFork, onRetryElsewhere }: {
  message: Message
  providers: Executor[]
  streaming?: { blocks: Block[]; usage: Usage | null }
  onFork: () => void
  onRetryElsewhere?: () => void
}) {
  if (message.role === 'user') {
    const files = attachmentsOf(message)
    return (
      <div className="group flex flex-col items-end">
        {files.length > 0 && <Attachments files={files} />}
        {textOf(message) && <div className="max-w-[85%] whitespace-pre-wrap break-words rounded-2xl bg-raised px-4 py-2.5">{textOf(message)}</div>}
        {textOf(message) && (
          <span className="mt-1 flex text-xs md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100">
            <CopyButton text={textOf(message)} />
          </span>
        )}
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
