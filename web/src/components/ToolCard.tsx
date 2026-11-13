import { useState } from 'react'
import {
  Brain, Check, ChevronDown, ChevronRight, FileText, Globe, Loader2, MoveRight, Pencil, Search, SquareTerminal, Trash2,
  Wrench, X,
} from 'lucide-react'
import type { ToolCall } from '../api'
import { diffStats } from '../blocks'

const ICONS: Record<string, React.ComponentType<{ className?: string }>> = {
  read: FileText, edit: Pencil, delete: Trash2, move: MoveRight, search: Search, execute: SquareTerminal,
  fetch: Globe, think: Brain,
}
const LIVE_LINES = 5

export function isReading(block: ToolCall): boolean {
  return (block.kind === 'read' || block.kind === 'search') && block.status === 'completed' && !block.note
}

function label(block: ToolCall): string {
  if (block.kind === 'execute' && block.command) return block.command
  return block.path ?? block.locations?.[0]?.path ?? block.title ?? block.kind
}

function Status({ block }: { block: ToolCall }) {
  if (block.status === 'completed') return <Check className="size-3.5 text-ok" aria-label="完了" />
  if (block.status === 'failed') return <X className="size-3.5 text-danger" aria-label="失敗" />
  return <Loader2 className="size-3.5 animate-spin text-muted" aria-label="実行中" />
}

export function DiffView({ diff }: { diff: string }) {
  return (
    <pre className="max-h-80 overflow-auto rounded-lg border border-border bg-surface p-2 font-mono text-xs leading-5">
      {diff.split('\n').filter((line) => !line.startsWith('Index:') && !line.startsWith('====')).map((line, index) => (
        <div
          key={index}
          className={line.startsWith('+') && !line.startsWith('+++') ? 'bg-ok/10 text-ok'
            : line.startsWith('-') && !line.startsWith('---') ? 'bg-danger/10 text-danger'
            : line.startsWith('@@') ? 'text-accent' : 'text-muted'}
        >
          {line || ' '}
        </div>
      ))}
    </pre>
  )
}

export function ToolCard({ block }: { block: ToolCall }) {
  const running = block.status !== 'completed' && block.status !== 'failed'
  const [open, setOpen] = useState(false)
  const Icon = ICONS[block.kind] ?? Wrench
  const stats = block.diff ? diffStats(block.diff) : null
  const tail = block.output ? block.output.trimEnd().split('\n').slice(-LIVE_LINES).join('\n') : ''
  const expandable = Boolean(block.diff || block.output)

  return (
    <div className="rounded-lg border border-border bg-surface/50 text-sm">
      <button
        onClick={() => expandable && setOpen((value) => !value)}
        className={`flex w-full items-center gap-2 px-2.5 py-1.5 text-left ${expandable ? '' : 'cursor-default'}`}
      >
        <Icon className="size-4 shrink-0 text-muted" />
        <span className={`min-w-0 flex-1 truncate ${block.kind === 'execute' ? 'font-mono text-[13px]' : ''}`}>
          {block.kind === 'execute' && <span className="text-muted">$ </span>}
          {label(block)}
        </span>
        {stats && (stats.added > 0 || stats.removed > 0) && (
          <span className="shrink-0 font-mono text-xs"><span className="text-ok">+{stats.added}</span> <span className="text-danger">−{stats.removed}</span></span>
        )}
        {block.note && <span className="shrink-0 text-xs text-warn">{block.note}</span>}
        <Status block={block} />
        {expandable && (open ? <ChevronDown className="size-4 shrink-0 text-muted" /> : <ChevronRight className="size-4 shrink-0 text-muted" />)}
      </button>
      {running && tail && !open && (
        <pre className="mx-2.5 mb-2 overflow-x-auto rounded bg-raised px-2 py-1 font-mono text-xs text-muted">{tail}</pre>
      )}
      {open && (
        <div className="space-y-2 px-2.5 pb-2.5">
          {block.diff && <DiffView diff={block.diff} />}
          {block.output && <pre className="max-h-80 overflow-auto rounded-lg bg-raised p-2 font-mono text-xs">{block.output}</pre>}
        </div>
      )}
    </div>
  )
}

export function ReadGroup({ blocks }: { blocks: ToolCall[] }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="rounded-lg border border-border bg-surface/50 text-sm">
      <button onClick={() => setOpen((value) => !value)} className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left">
        <FileText className="size-4 shrink-0 text-muted" />
        <span className="flex-1">{blocks.length}件を読んだ・検索した</span>
        <Check className="size-3.5 text-ok" />
        {open ? <ChevronDown className="size-4 text-muted" /> : <ChevronRight className="size-4 text-muted" />}
      </button>
      {open && (
        <ul className="px-2.5 pb-2 font-mono text-xs text-muted">
          {blocks.map((block) => <li key={block.id} className="truncate">{label(block)}</li>)}
        </ul>
      )}
    </div>
  )
}
