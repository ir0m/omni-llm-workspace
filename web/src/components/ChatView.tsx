import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { ArrowUp, File as FileIcon, FolderOpen, Menu, Paperclip, RefreshCw, Square, Timer, Wrench, X } from 'lucide-react'
import {
  api, attachmentsOf, attachmentUrl, textOf, type AppConfig, type AttachmentInfo, type ApprovalMode, type Draft, type Executor, type Mode,
  type McpServer, type PermissionAsk, type ProjectList, type Target,
} from '../api'
import { useConversation } from '../useConversation'
import { askToNotify } from '../notify'
import ApprovalCard from './ApprovalCard'
import ContextPanel from './ContextPanel'
import ExecutorPicker, { firstTarget, startTarget, usable } from './ExecutorPicker'
import MessageView from './MessageView'
import PlanBar from './PlanBar'
import ProjectPicker from './ProjectPicker'
import UsageMeter from './UsageMeter'
import ProjectPanel from './ProjectPanel'
import { useBackClose } from '../backClose'
import { attach, sizeLabel } from '../attach'

const LAST_TARGET = 'omni.lastTarget'
const AGENT_IDS = new Set(['codex', 'claude_code', 'claude_code_api', 'agy', 'opencode_free'])
const APPROVAL_LABEL: Record<ApprovalMode, string> = { ask: 'すべて確認', auto_edit: '編集は自動', auto: 'すべて自動' }
const LEVELS: ApprovalMode[] = ['ask', 'auto_edit', 'auto']
const AGENT_LABEL: Record<string, string> = {
  opencode: 'OpenCode', codex: 'Codex', claude_code: 'Claude Code', claude_code_api: 'Claude Code', agy: 'Antigravity',
}

function savedTarget(mode: Mode): Target | null {
  try {
    const saved = JSON.parse(localStorage.getItem(`${LAST_TARGET}.${mode}`) ?? 'null')
    return saved && typeof saved.provider === 'string' && typeof saved.model === 'string'
      ? { provider: saved.provider, model: saved.model, effort: typeof saved.effort === 'string' ? saved.effort : null } : null
  } catch {
    return null
  }
}

// What was typed survives switching conversations, leaving the page and reloads. A new conversation keeps one draft.
const INPUT = 'omni.input'
// Also retain drafts within this page when browser storage is disabled or full.
const inputs = new Map<string, string>()

function readInput(id: string | null): string {
  const key = `${INPUT}.${id ?? 'new'}`
  if (inputs.has(key)) return inputs.get(key)!
  try {
    return localStorage.getItem(key) ?? ''
  } catch {
    return ''
  }
}

function writeInput(id: string | null, text: string) {
  const key = `${INPUT}.${id ?? 'new'}`
  inputs.set(key, text)
  try {
    if (text) localStorage.setItem(key, text)
    else localStorage.removeItem(key)
  } catch {
    // The in-memory draft remains available until the page is reloaded.
  }
}

// Uploaded files waiting to be sent stay with their conversation while this page is open
type Pending = { key: string; name: string; size: number; preview: string | null; info: AttachmentInfo | null; error: string | null }
const pendingFiles = new Map<string, AttachmentInfo[]>()
const pendingKey = (id: string | null) => id ?? 'new'
const fromInfo = (info: AttachmentInfo): Pending => ({ key: info.id, name: info.name, size: info.size, info, error: null,
  preview: info.kind === 'image' ? attachmentUrl(info.id) : null })

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
  const { conversationId, executors, onError, onChanged, config } = props
  const { detail, setDetail, live, reconnecting, revision: panelRevision, load, follow, isActive } = useConversation(conversationId, props)
  const [target, setTarget] = useState<Target | null>(null)
  const [input, setInput] = useState(() => readInput(conversationId))
  useEffect(() => writeInput(conversationId, input), [conversationId, input])
  const [sending, setSending] = useState(false)
  const [pending, setPending] = useState<Pending[]>(() => (pendingFiles.get(pendingKey(conversationId)) ?? []).map(fromInfo))
  const [dragging, setDragging] = useState(false)
  const picker = useRef<HTMLInputElement>(null)
  useEffect(() => {
    const ready = pending.flatMap((item) => item.info ? [item.info] : [])
    if (ready.length) pendingFiles.set(pendingKey(conversationId), ready)
    else pendingFiles.delete(pendingKey(conversationId))
  }, [conversationId, pending])
  // Local previews of photos being uploaded are released when they leave the list or the view
  const previews = useRef(new Set<string>())
  useEffect(() => () => previews.current.forEach((url) => URL.revokeObjectURL(url)), [])
  const ready = pending.flatMap((item) => item.info ? [item.info] : [])
  const uploading = pending.some((item) => !item.info && !item.error)
  const [panelOpen, setPanelOpen] = useState(false)
  const [mcpServers, setMcpServers] = useState<McpServer[]>([])
  useEffect(() => {
    let active = true
    if (!config?.allow_mcp || !config.capabilities?.includes('mcp')) {
      setMcpServers([])
      return
    }
    api.mcp().then((data) => {
      if (active) setMcpServers(data.servers.filter((server) => server.enabled && server.usable))
    }).catch(() => { if (active) setMcpServers([]) })
    return () => { active = false }
  }, [config])
  // Below lg the panel covers the conversation: the back button closes it
  useBackClose(panelOpen && !window.matchMedia('(min-width: 1024px)').matches, () => setPanelOpen(false))
  const [followup, setFollowup] = useState<{ text: string; files: AttachmentInfo[] } | null>(null)
  // "Continue with another executor" after a usage limit: the message to send again and where to
  const [retry, setRetry] = useState<{ text: string; files: AttachmentInfo[]; target: Target | null } | null>(null)
  const sendLock = useRef(false)
  const inputValue = useRef(input)
  inputValue.current = input
  const scroller = useRef<HTMLDivElement>(null)
  const stick = useRef(true)
  const inputRequest = useRef<{ runId: string; id: string; text: string } | null>(null)
  const restored = useRef(new Set<string>())
  const canInput = config?.capabilities?.includes('run_input') ?? false

  const mode: Mode = detail ? detail.mode : props.draft.mode
  const project = detail ? detail.project : props.draft.project
  const temporary = detail ? detail.temporary : props.draft.temporary
  const approval: ApprovalMode = (detail ? detail.approval_mode : props.draft.approval_mode) ?? config?.approval_default ?? 'auto_edit'

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
    const chosen = !conversationId && initial ? executors.find((item) => item.id === initial?.provider) : undefined
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
    try { localStorage.setItem(`${LAST_TARGET}.${mode}`, JSON.stringify(next)) } catch { /* storage disabled or full */ }
  }

  async function change(patch: { mode?: Mode; project?: string; approval_mode?: ApprovalMode; clear_allow_rules?: boolean; mcp_off?: string[] }) {
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
  // MCP tools: Chat with an API or local model. Agents keep their own MCP settings.
  const mcpOff = (detail ? detail.mcp_off : props.draft.mcp_off) ?? []
  const mcpShown = mode === 'chat' && executor && !AGENT_IDS.has(executor.id) ? mcpServers : []
  const blocked = detail?.sync_error ? detail.sync_error
    : conversationId && !detail ? '会話を読み込んでいます'
    : mode === 'code' && !project ? 'プロジェクトを選んでください'
    : target && !executor ? '選択した実行元が見つかりません。実行元を選び直してください'
    : executor && !usable(executor, mode) ? (mode === 'code' ? 'この実行元は Code で使えません' : 'この実行元は Chat で使えません')
    : null

  function addFiles(list: FileList | File[]) {
    const chosen = Array.from(list).slice(0, Math.max(0, 10 - pending.length))
    if (list.length > chosen.length) onError(new Error('添付できるファイルは1回に10個までです'))
    for (const file of chosen) {
      const key = crypto.randomUUID()
      const preview = file.type.startsWith('image/') ? URL.createObjectURL(file) : null
      if (preview) previews.current.add(preview)
      setPending((current) => [...current, { key, name: file.name || '貼り付けた画像', size: file.size, preview, info: null, error: null }])
      attach(file).then((info) => {
        setPending((current) => current.map((item) => item.key === key ? { ...item, info, name: info.name, size: info.size } : item))
      }).catch((reason) => {
        setPending((current) => current.map((item) => item.key === key
          ? { ...item, error: reason instanceof Error ? reason.message : 'アップロードできませんでした' } : item))
      })
    }
  }

  function removeFile(key: string) {
    setPending((current) => {
      const item = current.find((entry) => entry.key === key)
      if (item?.preview && previews.current.has(item.preview)) {
        URL.revokeObjectURL(item.preview)
        previews.current.delete(item.preview)
      }
      return current.filter((entry) => entry.key !== key)
    })
  }

  const sendText = useCallback(async (text: string, files: AttachmentInfo[] = []) => {
    // Instructions added while an agent works are text only
    if (live) files = []
    if ((!text && !files.length) || !target || sendLock.current || sending || blocked || (live && !canInput)) return
    sendLock.current = true
    setSending(true)
    if (mode === 'code') askToNotify()
    let id = conversationId
    try {
      if (live) {
        const previous = inputRequest.current
        const attempt = previous?.runId === live.runId && previous.text === text ? previous
          : { runId: live.runId, id: crypto.randomUUID(), text }
        inputRequest.current = attempt
        const result = await api.input(live.runId, attempt.id, text)
        inputRequest.current = null
        if (!isActive()) return
        setInput((current) => current.trim() === text ? '' : current)
        if (result.status === 'returned') onError(new Error('追加指示の送信結果を確認できません。入力欄に戻します。履歴を確認してから再送してください。'))
        if (conversationId) await load(conversationId)
        return
      }
      if (!id) {
        id = (await api.create(props.draft)).id
        onChanged()
      }
      const started = await api.send(id, { text, provider: target.provider, model: target.model, effort: target.effort,
        attachments: files.map((file) => file.id) })
      const sent = new Set(files.map((file) => file.id))
      setPending((current) => current.filter((item) => !item.info || !sent.has(item.info.id)))
      onChanged()
      if (!isActive()) return
      setInput((current) => (current.trim() === text ? '' : current))
      stick.current = true
      if (id !== conversationId) {
        writeInput(id, inputValue.current.trim() === text ? '' : inputValue.current)
        writeInput(null, '')  // this view unmounts before its own effect would clear the draft
        pendingFiles.delete(pendingKey(null))
        props.onSelect(id)  // this view is replaced by one for the new conversation, which follows the run
      } else {
        setDetail((current) =>
          current ? { ...current, messages: [...current.messages, started.user_message, started.assistant_message] } : current,
        )
        follow(started.run_id, started.assistant_message.id)
      }
    } catch (error) {
      if (isActive()) {
        if (id && !conversationId) {
          // The conversation already exists even if sending failed. Open it with the draft
          // intact, so retrying cannot create duplicates and later settings changes apply to it.
          writeInput(id, inputValue.current)
          writeInput(null, '')
          if (files.length) pendingFiles.set(id, files)  // the view for the new conversation picks them up
          pendingFiles.delete(pendingKey(null))
          props.onSelect(id)
        }
        onError(error)
      }
    } finally {
      sendLock.current = false
      if (isActive()) setSending(false)
    }
  }, [target, sending, live, blocked, mode, conversationId, props, follow, onChanged, onError, canInput, load, isActive])

  // After "deny with a reason" stopped the turn, the reason becomes the next message
  useEffect(() => {
    if (!live && followup && !sending) {
      setFollowup(null)
      sendText(followup.text, followup.files)
    }
  }, [live, followup, sending, sendText])

  async function answer(ask: PermissionAsk, outcome: 'allow' | 'allow_always' | 'deny', reason?: string) {
    if (!live) return
    try {
      await api.answer(live.runId, ask.requestId, outcome, reason)
      if (outcome === 'deny' && reason) {
        setFollowup({ text: reason, files: [] })
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
    setRetry({ text: textOf(question), files: attachmentsOf(question), target: others.length ? { provider: others[0].id, model: others[0].models[0].id, effort: null } : null })
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
            agent={AGENT_LABEL[liveAgent ?? ''] ?? (ask.kind === 'mcp' ? 'モデル' : 'エージェント')}
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
                setFollowup({ text: retry.text, files: retry.files })
                setRetry(null)
              }}
              className="rounded-lg bg-accent px-3 py-1 font-medium text-accent-fg disabled:opacity-40"
            >
              送る
            </button>
            <button onClick={() => setRetry(null)} className="text-muted">やめる</button>
          </div>
        )}
        {config?.capabilities?.includes('context') && detail && !live && !sending && <ContextPanel detail={detail} target={target} onChanged={() => load(detail.id).catch(onError)} onError={onError} />}
        {blocked && <p className="mx-auto mb-2 max-w-3xl text-center text-xs text-warn">{blocked}</p>}
        {reconnecting && <p role="status" className="mx-auto mb-2 max-w-3xl text-center text-xs text-warn">接続が切れました。再接続しています…</p>}
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
        {mcpShown.length > 0 && (
          <div className="mx-auto mb-1.5 flex max-w-3xl flex-wrap items-center gap-1.5 text-xs" aria-label="この会話で使うMCPツール">
            <Wrench className="size-3.5 text-muted" aria-hidden />
            {mcpShown.map((server) => {
              const on = !mcpOff.includes(server.id)
              return (
                <button key={server.id} aria-pressed={on} title={on ? `${server.name} を使う（押すと外す）` : `${server.name} を使わない（押すと使う）`}
                  disabled={!!live || sending}
                  onClick={() => change({ mcp_off: on ? [...mcpOff, server.id] : mcpOff.filter((id) => id !== server.id) })}
                  className={`rounded-full border px-2 py-0.5 disabled:opacity-50 ${on ? 'border-accent text-accent' : 'border-border text-muted line-through'}`}>
                  {server.name}
                </button>
              )
            })}
          </div>
        )}
        <div className={`mx-auto max-w-3xl rounded-2xl border bg-surface p-2 focus-within:border-accent ${dragging ? 'border-accent' : 'border-border'}`}
          onDragOver={(event) => {
            if (live || !event.dataTransfer.types.includes('Files')) return
            event.preventDefault()
            setDragging(true)
          }}
          onDragLeave={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false) }}
          onDrop={(event) => {
            setDragging(false)
            if (live || !event.dataTransfer.files.length) return
            event.preventDefault()
            addFiles(event.dataTransfer.files)
          }}>
          {pending.length > 0 && <ul className="mb-2 flex flex-wrap gap-2" aria-label="添付するファイル">
            {pending.map((item) => <li key={item.key}
              className={`flex max-w-full items-center gap-2 rounded-xl border bg-bg py-1 pl-1 pr-1.5 text-xs ${item.error ? 'border-danger' : 'border-border'}`}>
              {item.preview ? <img src={item.preview} alt="" className="size-9 shrink-0 rounded-lg object-cover" />
                : <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-raised"><FileIcon className="size-4 text-muted" aria-hidden /></span>}
              <span className="flex min-w-0 flex-col">
                <span className="max-w-[12rem] truncate" title={item.name}>{item.name}</span>
                <span className={item.error ? 'text-danger' : 'text-muted'} role={item.error ? 'alert' : undefined}>
                  {item.error ?? (item.info ? sizeLabel(item.size) : 'アップロード中…')}
                </span>
              </span>
              <button onClick={() => removeFile(item.key)} aria-label={`${item.name} を外す`} title="外す"
                className="shrink-0 rounded p-1 text-muted hover:text-fg"><X className="size-3.5" aria-hidden /></button>
            </li>)}
          </ul>}
          <div className="flex items-end gap-2">
          <input ref={picker} type="file" multiple hidden onChange={(event) => {
            if (event.target.files?.length) addFiles(event.target.files)
            event.target.value = ''  // choosing the same file again still adds it
          }} />
          <button onClick={() => picker.current?.click()} disabled={!!live || sending || pending.length >= 10}
            className="rounded-xl p-2 text-muted hover:text-fg disabled:opacity-40" aria-label="ファイルを添付"
            title={live ? '応答中は添付できません' : 'ファイルを添付（ドラッグ＆ドロップ・画像の貼り付けもできます）'}>
            <Paperclip className="size-5" aria-hidden />
          </button>
          <textarea
            aria-label="メッセージ"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onPaste={(event) => {
              if (live || !event.clipboardData.files.length) return
              event.preventDefault()  // a pasted screenshot becomes an attachment, not text
              addFiles(event.clipboardData.files)
            }}
            onKeyDown={(event) => {
              // Enter sends on a keyboard; on touch screens Enter is a new line. Never while the IME is composing.
              if (event.key === 'Enter' && !event.shiftKey && !touch && !event.nativeEvent.isComposing && event.keyCode !== 229) {
                event.preventDefault()
                if (!uploading) sendText(input.trim(), ready)
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
              onClick={() => sendText(input.trim(), ready)}
              disabled={(!input.trim() && (!!live || !ready.length)) || uploading || !target || sending || blocked !== null}
              className="rounded-xl bg-accent p-2 text-accent-fg disabled:opacity-40"
              aria-label="送信"
              title={uploading ? 'アップロードの完了を待っています' : '送信'}
            >
              <ArrowUp className="size-5" aria-hidden />
            </button>
          )}
          </div>
        </div>
        <UsageMeter executor={executor} />
      </div>
    </main>
    {panelOpen && mode === 'code' && project && <ProjectPanel key={project} project={project} revision={panelRevision} capabilities={config?.capabilities ?? []} onChanged={() => { props.onProjectsChanged(); onChanged() }} onClose={() => setPanelOpen(false)} />}
    </div>
  )
}
