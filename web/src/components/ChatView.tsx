import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { ArrowUp, FolderOpen, Menu, RefreshCw, Square, Timer, X } from 'lucide-react'
import {
  api, ApiError, textOf, type AppConfig, type ApprovalMode, type Block, type ConversationDetail, type Draft, type Executor, type Mode,
  type PermissionAsk, type ProjectList, type Target, type Usage,
} from '../api'
import { applyUpdate, mergeUsage } from '../blocks'
import { askToNotify, notify } from '../notify'
import ApprovalCard from './ApprovalCard'
import ExecutorPicker, { firstTarget, startTarget, usable } from './ExecutorPicker'
import MessageView from './MessageView'
import PlanBar from './PlanBar'
import ProjectPicker from './ProjectPicker'
import UsageMeter from './UsageMeter'
import ProjectPanel from './ProjectPanel'

const LAST_TARGET = 'omni.lastTarget'
const APPROVAL_LABEL: Record<ApprovalMode, string> = { ask: 'すべて確認', auto_edit: '編集は自動', auto: 'すべて自動' }
const LEVELS: ApprovalMode[] = ['ask', 'auto_edit', 'auto']
const AGENT_LABEL: Record<string, string> = {
  opencode: 'OpenCode', codex: 'Codex', claude_code: 'Claude Code', claude_code_api: 'Claude Code',
}

type Live = { runId: string; messageId: string; blocks: Block[]; usage: Usage | null; permissions: PermissionAsk[] }

function savedTarget(mode: Mode): Target | null {
  try {
    return JSON.parse(localStorage.getItem(`${LAST_TARGET}.${mode}`) ?? 'null')
  } catch {
    return null
  }
}

// What was typed survives switching conversations, leaving the page and reloads. A new conversation keeps one draft.
const INPUT = 'omni.input'

function readInput(id: string | null): string {
  try {
    return localStorage.getItem(`${INPUT}.${id ?? 'new'}`) ?? ''
  } catch {
    return ''
  }
}

function writeInput(id: string | null, text: string) {
  try {
    if (text) localStorage.setItem(`${INPUT}.${id ?? 'new'}`, text)
    else localStorage.removeItem(`${INPUT}.${id ?? 'new'}`)
  } catch {
    // storage full or disabled: the text simply isn't kept
  }
}

const touch = typeof window !== 'undefined' && window.matchMedia?.('(pointer: coarse)').matches

type Props = {
  conversationId: string | null
  draft: Draft
  onDraftChange: (draft: Draft) => void
  executors: Executor[]
  projects: ProjectList | null
  config: AppConfig | null
  onProjectsChanged: () => void
  onRefreshExecutors: () => void
  onOpenMenu: () => void
  onSelect: (id: string | null) => void
  onChanged: () => void
  onRunFinished: () => void
  onError: (error: unknown) => void
}

function ModeToggle({ mode, onChange }: { mode: Mode; onChange: (mode: Mode) => void }) {
  return (
    <div className="flex shrink-0 rounded-lg border border-border p-0.5 text-sm" role="radiogroup" aria-label="モード">
      {(['chat', 'code'] as const).map((item) => (
        <button
          key={item}
          role="radio"
          aria-checked={mode === item}
          onClick={() => onChange(item)}
          className={`rounded-md px-2.5 py-1 ${mode === item ? 'bg-accent text-accent-fg' : 'text-muted hover:text-fg'}`}
        >
          {item === 'chat' ? 'Chat' : 'Code'}
        </button>
      ))}
    </div>
  )
}

export default function ChatView(props: Props) {
  const { conversationId, executors, onError, onChanged, onSelect, config } = props
  const [detail, setDetail] = useState<ConversationDetail | null>(null)
  const [live, setLive] = useState<Live | null>(null)
  const [target, setTarget] = useState<Target | null>(null)
  const [input, setInput] = useState(() => readInput(conversationId))
  useEffect(() => writeInput(conversationId, input), [conversationId, input])
  const [sending, setSending] = useState(false)
  const [panelOpen, setPanelOpen] = useState(false)
  const [panelRevision, setPanelRevision] = useState(0)
  const [followup, setFollowup] = useState<string | null>(null)
  // "Continue with another executor" after a usage limit: the message to send again and where to
  const [retry, setRetry] = useState<{ text: string; target: Target | null } | null>(null)
  const source = useRef<EventSource | null>(null)
  const scroller = useRef<HTMLDivElement>(null)
  const stick = useRef(true)
  const inputRequest = useRef<{ runId: string; id: string; text: string } | null>(null)
  const restored = useRef(new Set<string>())
  const canInput = config?.capabilities?.includes('run_input') ?? false

  const mode: Mode = detail ? detail.mode : props.draft.mode
  const project = detail ? detail.project : props.draft.project
  const temporary = detail ? detail.temporary : props.draft.temporary
  const approval: ApprovalMode = (detail ? detail.approval_mode : props.draft.approval_mode) ?? config?.approval_default ?? 'auto_edit'

  const load = useCallback(
    (id: string) =>
      api.conversation(id).then((data) => {
        setDetail(data)
        return data
      }),
    [],
  )

  const follow = useCallback(
    (runId: string, messageId: string) => {
      source.current?.close()
      setLive({ runId, messageId, blocks: [], usage: null, permissions: [] })
      const events = new EventSource(`/api/runs/${runId}/events`)
      source.current = events
      const change = (update: (current: Live) => Live) =>
        setLive((current) => (current && current.runId === runId ? update(current) : current))
      events.addEventListener('update', (event) => {
        const update = JSON.parse((event as MessageEvent).data)
        change((current) => update.sessionUpdate === 'usage_update'
          ? { ...current, usage: mergeUsage(current.usage, update) }
          : { ...current, blocks: applyUpdate(current.blocks, update) })
      })
      events.addEventListener('permission', (event) => {
        const ask: PermissionAsk = JSON.parse((event as MessageEvent).data)
        change((current) => ({ ...current, permissions: [...current.permissions.filter((item) => item.requestId !== ask.requestId), ask] }))
        notify('承認が必要です', ask.command ?? (ask.paths.join(', ') || ask.title))
      })
      events.addEventListener('permission_resolved', (event) => {
        const { requestId } = JSON.parse((event as MessageEvent).data)
        change((current) => ({ ...current, permissions: current.permissions.filter((item) => item.requestId !== requestId) }))
      })
      events.addEventListener('input_changed', () => {
        if (conversationId) load(conversationId).catch(onError)
      })
      const finish = () => {
        events.close()
        if (source.current === events) source.current = null
        if (conversationId) load(conversationId).then((data) => {
          const streaming = data.messages.findLast((message) => message.status === 'streaming')
          if (data.active_run && streaming) follow(data.active_run, streaming.id)
          else setLive(null)
        }).catch(onError)
        else setLive(null)
        onChanged()
        props.onRunFinished()  // usage windows move after every turn
        setPanelRevision((value) => value + 1)
      }
      events.addEventListener('status', (event) => {
        const status = JSON.parse((event as MessageEvent).data).status
        if (status !== 'running') {
          notify(status === 'complete' ? '応答が完了しました' : '応答が止まりました', document.title)
          finish()
        }
      })
      events.onerror = () => {
        if (events.readyState === EventSource.CLOSED) finish()  // the run is gone; show what was saved
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [conversationId, load, onChanged, onError],
  )

  useEffect(() => {
    if (!conversationId) {
      setDetail(null)
      return
    }
    load(conversationId)
      .then((data) => {
        const streaming = data.messages.findLast((message) => message.status === 'streaming')
        if (data.active_run && streaming) follow(data.active_run, streaming.id)
      })
      .catch((caught) => {
        // A link to a deleted conversation would fail again on every reload: leave it
        if (caught instanceof ApiError && caught.status === 404) onSelect(null)
        onError(caught)
      })
    return () => source.current?.close()
  }, [conversationId, load, follow, onError, onSelect])

  // Persist the recovered text before acknowledging it, including when the screen was closed at completion.
  useEffect(() => {
    if (!conversationId || sending) return
    const returned = detail?.pending_inputs?.filter((item) => item.status === 'returned') ?? []
    for (const item of returned) {
      const key = `${INPUT}.restored.${item.id}`
      let saved = false
      try { saved = localStorage.getItem(key) === '1' } catch { /* storage disabled */ }
      if (!restored.current.has(item.id) && !saved) {
        restored.current.add(item.id)
        setInput((current) => {
          const next = current ? `${current}\n\n${item.text}` : item.text
          writeInput(conversationId, next)
          try { localStorage.setItem(key, '1') } catch { /* storage disabled */ }
          return next
        })
      }
      api.removeInput(conversationId, item.id).then(() => {
        try { localStorage.removeItem(key) } catch { /* storage disabled */ }
        setDetail((current) => current ? {
          ...current, pending_inputs: current.pending_inputs?.filter((entry) => entry.id !== item.id),
        } : current)
      }).catch(onError)
    }
  }, [conversationId, detail?.pending_inputs, onError, sending])

  // Start from the conversation's last choice, else the last choice in this mode, else the first usable one
  useEffect(() => {
    if (target) return
    if (conversationId && !detail) return
    let initial = detail?.target ?? savedTarget(mode) ?? firstTarget(executors, mode)
    // A new conversation keeps the last executor but starts with that executor's default model, if one is set
    const chosen = !detail?.target && initial ? executors.find((item) => item.id === initial?.provider) : undefined
    if (chosen?.default) initial = startTarget(chosen)
    if (initial && !initial.model) {
      const model = executors.find((item) => item.id === initial?.provider)?.models[0]?.id
      if (!model) return
      initial = { ...initial, model }
    }
    if (initial) setTarget(initial)
  }, [conversationId, detail, executors, target, mode])

  useEffect(() => {
    const focus = () => {
      if (conversationId && !live && !sending) load(conversationId).catch(onError)
    }
    window.addEventListener('focus', focus)
    return () => window.removeEventListener('focus', focus)
  }, [conversationId, live, sending, load, onError])

  useLayoutEffect(() => {
    const element = scroller.current
    if (element && stick.current) element.scrollTop = element.scrollHeight
  })

  // The tab title shows what is waiting
  useEffect(() => {
    const base = detail?.title ? `${detail.title} · OmniLLMWorkspace` : 'OmniLLMWorkspace'
    document.title = live?.permissions.length ? `（承認待ち）${base}` : live ? `（応答中）${base}` : base
  }, [live, detail?.title])

  function chooseTarget(next: Target) {
    setTarget(next)
    localStorage.setItem(`${LAST_TARGET}.${mode}`, JSON.stringify(next))
  }

  async function change(patch: { mode?: Mode; project?: string; approval_mode?: ApprovalMode; clear_allow_rules?: boolean }) {
    if (!detail) {
      const { clear_allow_rules: _clear, ...rest } = patch
      props.onDraftChange({ ...props.draft, ...rest })
      return
    }
    try {
      setDetail({ ...detail, ...(await api.update(detail.id, patch)) })
      onChanged()
    } catch (error) {
      onError(error)
    }
  }

  function changeMode(next: Mode) {
    if (next === mode) return
    const current = executors.find((item) => item.id === target?.provider)
    if (current && !usable(current, next)) setTarget(savedTarget(next) ?? firstTarget(executors, next))
    change({ mode: next })
  }

  const executor = executors.find((item) => item.id === target?.provider)
  const blocked = detail?.sync_error ? detail.sync_error
    : conversationId && !detail ? '会話を読み込んでいます'
    : mode === 'code' && !project ? 'プロジェクトを選んでください'
    : executor && !usable(executor, mode) ? (mode === 'code' ? 'この実行元は Code で使えません' : 'この実行元は Chat で使えません')
    : null

  const sendText = useCallback(async (text: string) => {
    if (!text || !target || sending || blocked || (live && !canInput)) return
    setSending(true)
    if (mode === 'code') askToNotify()
    try {
      if (live) {
        const previous = inputRequest.current
        const attempt = previous?.runId === live.runId && previous.text === text ? previous
          : { runId: live.runId, id: crypto.randomUUID(), text }
        inputRequest.current = attempt
        const result = await api.input(live.runId, attempt.id, text)
        inputRequest.current = null
        setInput((current) => current.trim() === text ? '' : current)
        if (result.status === 'returned') onError(new Error('追加指示の送信結果を確認できません。入力欄に戻します。履歴を確認してから再送してください。'))
        if (conversationId) await load(conversationId)
        return
      }
      let id = conversationId
      if (!id) id = (await api.create(props.draft)).id
      const started = await api.send(id, { text, provider: target.provider, model: target.model, effort: target.effort })
      setInput((current) => (current.trim() === text ? '' : current))
      stick.current = true
      if (id !== conversationId) {
        writeInput(null, '')  // this view unmounts before its own effect would clear the draft
        props.onSelect(id)  // this view is replaced by one for the new conversation, which follows the run
      } else {
        setDetail((current) =>
          current ? { ...current, messages: [...current.messages, started.user_message, started.assistant_message] } : current,
        )
        follow(started.run_id, started.assistant_message.id)
      }
      onChanged()
    } catch (error) {
      onError(error)
    } finally {
      setSending(false)
    }
  }, [target, sending, live, blocked, mode, conversationId, props, follow, onChanged, onError, canInput, load])

  // After "deny with a reason" stopped the turn, the reason becomes the next message
  useEffect(() => {
    if (!live && followup && !sending) {
      setFollowup(null)
      sendText(followup)
    }
  }, [live, followup, sending, sendText])

  async function answer(ask: PermissionAsk, outcome: 'allow' | 'allow_always' | 'deny', reason?: string) {
    if (!live) return
    try {
      await api.answer(live.runId, ask.requestId, outcome, reason)
      if (outcome === 'deny' && reason) {
        setFollowup(reason)
        await api.cancel(live.runId)
      }
      if (outcome === 'allow_always' && conversationId) load(conversationId).catch(onError)
    } catch (error) {
      onError(error)
    }
  }

  function retryElsewhere(replyIndex: number) {
    const question = messages.slice(0, replyIndex).findLast((message) => message.role === 'user')
    if (!question) return
    const others = executors.filter((item) => item.id !== target?.provider && usable(item, mode))
    setRetry({ text: textOf(question), target: others.length ? { provider: others[0].id, model: others[0].models[0].id, effort: null } : null })
  }

  async function fork(messageId: string) {
    if (!conversationId) return
    try {
      const copy = await api.fork(conversationId, messageId)
      onChanged()
      props.onSelect(copy.id)
    } catch (error) {
      onError(error)
    }
  }

  const messages = detail?.messages ?? []
  const nothingUsable = executors.length > 0 && !executors.some((item) => usable(item, 'chat') || usable(item, 'code'))
  const plan = live?.blocks.find((block) => block.type === 'plan')
  const ask = live?.permissions[0]
  const liveAgent = messages.find((message) => message.id === live?.messageId)?.agent
  const maxLevel = LEVELS.indexOf(config?.approval_max ?? 'auto')
  const rules = detail?.allow_rules ?? []

  const controls = (
    <fieldset disabled={!!live || sending} className="flex min-w-0 items-center gap-2 disabled:opacity-60">
      <ModeToggle mode={mode} onChange={changeMode} />
      {mode === 'code' && (
        <ProjectPicker
          list={props.projects}
          value={project}
          onChange={(name) => change({ project: name, mode: 'code' })}
          onCreated={(name) => {
            props.onProjectsChanged()
            change({ project: name, mode: 'code' })
          }}
        />
      )}
      <ExecutorPicker executors={executors} mode={mode} target={target} onChange={chooseTarget} onRefresh={props.onRefreshExecutors} />
      {mode === 'code' && (
        <select
          value={approval}
          onChange={(event) => {
            const value = event.target.value
            if (value === '__clear') change({ clear_allow_rules: true })
            else change({ approval_mode: value as ApprovalMode })
          }}
          className="shrink-0 rounded-lg border border-border bg-bg px-2 py-1.5 text-sm"
          aria-label="承認"
          title="この会話での承認のしかた（.env の上限まで）"
        >
          {LEVELS.map((level, index) => (
            <option key={level} value={level} disabled={index > maxLevel}>承認: {APPROVAL_LABEL[level]}</option>
          ))}
          {rules.length > 0 && <option value="__clear">「常に許可」の記憶を消す（{rules.length}件）</option>}
        </select>
      )}
    </fieldset>
  )

  return (
    <div className="relative flex min-w-0 flex-1">
    <main className="flex min-w-0 flex-1 flex-col">
      <header className="border-b border-border">
        <div className="flex items-center gap-2 px-3 py-2">
          <button onClick={props.onOpenMenu} className="rounded-lg p-1.5 text-muted md:hidden" aria-label="会話一覧">
            <Menu className="size-5" aria-hidden />
          </button>
          <h1 className="min-w-0 truncate text-sm font-medium">{detail?.title || (mode === 'code' ? '新しいCode' : '新しいチャット')}</h1>
          {conversationId && <button onClick={() => load(conversationId).catch(onError)} disabled={!!live || sending}
            className="shrink-0 rounded-lg p-1.5 text-muted hover:text-fg disabled:opacity-40"
            aria-label="会話を更新" title="会話を更新">
            <RefreshCw className="size-4" aria-hidden />
          </button>}
          {temporary && (
            <span className="flex shrink-0 items-center gap-1 rounded-full bg-warn/15 px-2 py-0.5 text-xs text-warn" title="アプリには保存します。エージェント側には記録やメモリを残しません">
              <Timer className="size-3" aria-hidden /> 一時
            </span>
          )}
          <div className={`ml-auto hidden min-w-0 items-center gap-2 ${panelOpen && mode === 'code' && project ? '' : '2xl:flex'}`}>{controls}</div>
          {mode === 'code' && project && config?.capabilities?.includes('project_files') && <button onClick={() => setPanelOpen((open) => !open)}
            aria-label="ファイル・差分を開く" title="ファイル・差分" aria-expanded={panelOpen}
            className={`ml-auto shrink-0 rounded-lg p-2 ${panelOpen ? 'bg-raised text-accent' : 'text-muted hover:text-fg 2xl:ml-0'}`}>
            <FolderOpen className="size-5" aria-hidden />
          </button>}
        </div>
        <div className={`flex items-center gap-2 overflow-x-auto px-3 pb-2 ${panelOpen && mode === 'code' && project ? '' : '2xl:hidden'}`}>{controls}</div>
      </header>

      <div
        ref={scroller}
        onScroll={(event) => {
          const element = event.currentTarget
          stick.current = element.scrollHeight - element.scrollTop - element.clientHeight < 80
        }}
        className="flex-1 overflow-y-auto"
      >
        <div className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-6">
          {messages.length === 0 && (
            <div className="py-16 text-center text-muted">
              {nothingUsable ? (
                <p>使えるモデルがありません。.env に API キーかローカルLLMの URL を書いて再起動してください。</p>
              ) : (
                <>
                  <p className="text-lg text-fg">{mode === 'code' ? (project ? `${project} で何をしますか` : 'プロジェクトを選んでください') : '何でも聞いてください'}</p>
                  <p className="mt-1 text-sm">実行元とモデルは送るたびに変えられます。</p>
                  {!conversationId && (
                    <label className="mt-4 inline-flex items-center gap-2 text-sm">
                      <input type="checkbox" checked={props.draft.temporary} onChange={(event) => props.onDraftChange({ ...props.draft, temporary: event.target.checked })} />
                      一時チャットにする
                    </label>
                  )}
                </>
              )}
            </div>
          )}
          {messages.map((message, index) => (
            <MessageView
              key={message.id}
              message={message}
              providers={executors}
              streaming={live && live.messageId === message.id ? live : undefined}
              onFork={() => fork(message.id)}
              onRetryElsewhere={index === messages.length - 1 && !live ? () => retryElsewhere(index) : undefined}
            />
          ))}
        </div>
      </div>

      <div className="border-t border-border bg-bg px-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] pt-3">
        {plan && plan.type === 'plan' && plan.entries.length > 0 && <PlanBar entries={plan.entries} />}
        {ask && (
          <ApprovalCard
            key={ask.requestId}
            ask={ask}
            agent={AGENT_LABEL[liveAgent ?? ''] ?? 'エージェント'}
            project={project}
            onAnswer={(outcome, reason) => answer(ask, outcome, reason)}
          />
        )}
        {retry && (
          <div className="mx-auto mb-2 flex max-w-3xl flex-wrap items-center gap-2 rounded-xl border border-border bg-surface px-3 py-2 text-sm">
            <span className="shrink-0">別の実行元で送り直す:</span>
            <ExecutorPicker
              executors={executors.filter((item) => item.id !== target?.provider)}
              mode={mode}
              target={retry.target}
              onChange={(next) => setRetry({ ...retry, target: next })}
              onRefresh={props.onRefreshExecutors}
            />
            <button
              disabled={!retry.target}
              onClick={() => {
                if (!retry.target) return
                chooseTarget(retry.target)
                setFollowup(retry.text)
                setRetry(null)
              }}
              className="rounded-lg bg-accent px-3 py-1 font-medium text-accent-fg disabled:opacity-40"
            >
              送る
            </button>
            <button onClick={() => setRetry(null)} className="text-muted">やめる</button>
          </div>
        )}
        {blocked && <p className="mx-auto mb-2 max-w-3xl text-center text-xs text-warn">{blocked}</p>}
        {!!detail?.pending_inputs?.some((item) => item.status === 'queued') && (
          <div className="mx-auto mb-2 max-h-28 max-w-3xl overflow-y-auto rounded-xl border border-border px-3 py-2 text-sm">
            {detail.pending_inputs.filter((item) => item.status === 'queued').map((item) => (
              <div key={item.id} className="flex items-start gap-2 py-1">
                <span className="min-w-0 flex-1 whitespace-pre-wrap break-words"><span className="text-muted">終わったら送る: </span>{item.text}</span>
                <button aria-label="予約を取り消す" className="shrink-0 rounded p-1 text-muted hover:text-fg" onClick={() => {
                  if (conversationId) api.removeInput(conversationId, item.id).then((result) => {
                    if (!result.removed) onError(new Error('この予約はすでに処理されています。会話を確認してください。'))
                    return load(conversationId)
                  }).catch(onError)
                }}><X className="size-4" aria-hidden /></button>
              </div>
            ))}
          </div>
        )}
        <div className="mx-auto flex max-w-3xl items-end gap-2 rounded-2xl border border-border bg-surface p-2 focus-within:border-accent">
          <textarea
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={(event) => {
              // Enter sends on a keyboard; on touch screens Enter is a new line. Never while the IME is composing.
              if (event.key === 'Enter' && !event.shiftKey && !touch && !event.nativeEvent.isComposing) {
                event.preventDefault()
                sendText(input.trim())
              }
            }}
            rows={Math.min(8, Math.max(1, input.split('\n').length))}
            placeholder={live && canInput ? '作業中の追加指示を入力' : target ? 'メッセージを入力' : '実行元とモデルを選んでください'}
            className="max-h-60 min-h-[2.25rem] flex-1 resize-none bg-transparent px-2 py-1.5 outline-none"
          />
          {live && (
            <button onClick={() => api.cancel(live.runId).catch(onError)} className="rounded-xl bg-raised p-2" aria-label="停止" title="停止">
              <Square className="size-5" aria-hidden />
            </button>
          )}
          {(!live || canInput) && (
            <button
              onClick={() => sendText(input.trim())}
              disabled={!input.trim() || !target || sending || blocked !== null}
              className="rounded-xl bg-accent p-2 text-accent-fg disabled:opacity-40"
              aria-label="送信"
              title="送信"
            >
              <ArrowUp className="size-5" aria-hidden />
            </button>
          )}
        </div>
        <UsageMeter executor={executor} />
      </div>
    </main>
    {panelOpen && mode === 'code' && project && <ProjectPanel key={project} project={project} revision={panelRevision} onClose={() => setPanelOpen(false)} />}
    </div>
  )
}
