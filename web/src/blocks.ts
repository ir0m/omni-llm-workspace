/** The same folding rules as omniws/blocks.py, so a reply looks the same while it streams and once saved. */
import type { Block, Usage } from './api'

const TOOL_FIELDS = ['title', 'kind', 'status', 'locations', 'command', 'path', 'diff', 'output', 'note'] as const

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type Update = Record<string, any> & { sessionUpdate: string }

export function applyUpdate(blocks: Block[], update: Update): Block[] {
  const next = [...blocks]
  const kind = update.sessionUpdate
  if (kind === 'agent_message_chunk' || kind === 'agent_thought_chunk') {
    const type = kind === 'agent_message_chunk' ? 'text' : 'thinking'
    const text = update.content?.text ?? ''
    const last = next[next.length - 1]
    if (last && last.type === type) next[next.length - 1] = { ...last, text: last.text + text }
    else next.push({ type, text })
  } else if (kind === 'user_message_chunk') {
    next.push({ type: 'user_input', text: update.content?.text ?? '' })
  } else if (kind === 'tool_call' || kind === 'tool_call_update') {
    const index = next.findIndex((block) => block.type === 'tool_call' && block.id === update.toolCallId)
    const base: Block = index >= 0 ? next[index] : { type: 'tool_call', id: update.toolCallId, title: '', kind: 'other', status: 'pending' }
    const merged = { ...base } as Record<string, unknown>
    for (const field of TOOL_FIELDS) if (update[field] !== undefined && update[field] !== null) merged[field] = update[field]
    if (index >= 0) next[index] = merged as Block
    else next.push(merged as Block)
  } else if (kind === 'plan') {
    const index = next.findIndex((block) => block.type === 'plan')
    const block: Block = { type: 'plan', entries: update.entries ?? [] }
    if (index >= 0) next[index] = block
    else next.unshift(block)
  } else if (kind === 'handover') {
    next.push({ type: 'handover', agent: update.agent, count: update.count ?? 0 })
  }
  return next
}

export function mergeUsage(usage: Usage | null, update: Update): Usage {
  const { sessionUpdate: _kind, ...rest } = update
  const merged: Record<string, unknown> = { ...(usage ?? {}) }
  for (const [key, value] of Object.entries(rest)) if (value !== null && value !== undefined) merged[key] = value
  return merged as Usage
}

export function diffStats(diff: string | undefined): { added: number; removed: number } {
  let added = 0
  let removed = 0
  for (const line of (diff ?? '').split('\n')) {
    if (line.startsWith('+') && !line.startsWith('+++')) added++
    else if (line.startsWith('-') && !line.startsWith('---')) removed++
  }
  return { added, removed }
}
