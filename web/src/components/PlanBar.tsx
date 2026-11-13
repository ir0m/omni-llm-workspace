import { useState } from 'react'
import { CheckSquare, ChevronDown, ChevronUp, Circle, CircleDot, Square } from 'lucide-react'
import type { PlanEntry } from '../api'

export function PlanList({ entries }: { entries: PlanEntry[] }) {
  return (
    <ul className="space-y-1 text-sm">
      {entries.map((entry, index) => (
        <li key={index} className={`flex items-start gap-2 ${entry.status === 'completed' ? 'text-muted line-through' : ''}`}>
          {entry.status === 'completed' ? <CheckSquare className="mt-0.5 size-4 shrink-0 text-ok" />
            : entry.status === 'in_progress' ? <CircleDot className="mt-0.5 size-4 shrink-0 text-accent" />
            : <Square className="mt-0.5 size-4 shrink-0 text-muted" />}
          <span>{entry.content}</span>
        </li>
      ))}
    </ul>
  )
}

function progress(entries: PlanEntry[]) {
  const done = entries.filter((entry) => entry.status === 'completed').length
  const current = entries.find((entry) => entry.status === 'in_progress') ?? entries.find((entry) => entry.status === 'pending')
  return { done, current }
}

/** Above the composer while a turn runs: "2/4 テストを走らせる"; tap for the whole list. */
export default function PlanBar({ entries }: { entries: PlanEntry[] }) {
  const [open, setOpen] = useState(false)
  const { done, current } = progress(entries)
  return (
    <div className="mx-auto mb-2 max-w-3xl rounded-xl border border-border bg-surface text-sm">
      <button onClick={() => setOpen((value) => !value)} className="flex w-full items-center gap-2 px-3 py-1.5 text-left">
        <span className="shrink-0 font-medium">作業計画 {done}/{entries.length}</span>
        <span className="min-w-0 flex-1 truncate text-muted">{current?.content ?? '完了'}</span>
        {open ? <ChevronDown className="size-4 text-muted" /> : <ChevronUp className="size-4 text-muted" />}
      </button>
      {open && <div className="border-t border-border px-3 py-2"><PlanList entries={entries} /></div>}
    </div>
  )
}

/** In a finished reply: a collapsed summary of the plan. */
export function PlanSummary({ entries }: { entries: PlanEntry[] }) {
  const [open, setOpen] = useState(false)
  const { done } = progress(entries)
  return (
    <div className="text-sm">
      <button onClick={() => setOpen((value) => !value)} className="flex items-center gap-1.5 text-muted hover:text-fg">
        <Circle className="size-3.5" /> 作業計画 {done}/{entries.length}
        {open ? <ChevronUp className="size-3.5" /> : <ChevronDown className="size-3.5" />}
      </button>
      {open && <div className="mt-1.5 rounded-lg border border-border px-3 py-2"><PlanList entries={entries} /></div>}
    </div>
  )
}
