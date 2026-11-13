import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { ArrowUp, Menu, Square, Timer } from 'lucide-react'
import { api, type ConversationDetail, type Provider, type Target, type Usage } from '../api'
import MessageView from './MessageView'
import ModelPicker, { firstTarget } from './ModelPicker'

const LAST_TARGET = 'omni.lastTarget'

type Live = { runId: string; messageId: string; text: string; thought: string; usage: Usage | null }

function savedTarget(): Target | null {
  try {
    return JSON.parse(localStorage.getItem(LAST_TARGET) ?? 'null')
  } catch {
    return null
  }
}

const touch = typeof window !== 'undefined' && window.matchMedia?.('(pointer: coarse)').matches

type Props = {
  conversationId: string | null
  newTemporary: boolean
  onNewTemporaryChange: (value: boolean) => void
  providers: Provider[]
  onRefreshProviders: () => void
  onOpenMenu: () => void
  onSelect: (id: string | null) => void
  onChanged: () => void
  onError: (error: unknown) => void
}

export default function ChatView(props: Props) {
  const { conversationId, providers, onError, onChanged } = props
  const [detail, setDetail] = useState<ConversationDetail | null>(null)
  const [live, setLive] = useState<Live | null>(null)
  const [target, setTarget] = useState<Target | null>(null)
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const source = useRef<EventSource | null>(null)
  const scroller = useRef<HTMLDivElement>(null)
  const stick = useRef(true)

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
      setLive({ runId, messageId, text: '', thought: '', usage: null })
      const events = new EventSource(`/api/runs/${runId}/events`)
      source.current = events
      events.addEventListener('update', (event) => {
        const update = JSON.parse((event as MessageEvent).data)
        setLive((current) => {
          if (!current || current.runId !== runId) return current
          if (update.sessionUpdate === 'agent_message_chunk') return { ...current, text: current.text + update.content.text }
          if (update.sessionUpdate === 'agent_thought_chunk') return { ...current, thought: current.thought + update.content.text }
          if (update.sessionUpdate === 'usage_update') {
            const { sessionUpdate: _kind, ...usage } = update
            return { ...current, usage }
          }
          return current
        })
      })
      const finish = () => {
        events.close()
        if (source.current === events) source.current = null
        if (conversationId) load(conversationId).finally(() => setLive(null)).catch(onError)
        onChanged()
      }
      events.addEventListener('status', (event) => {
        const status = JSON.parse((event as MessageEvent).data).status
        if (status !== 'running') finish()
      })
      events.onerror = () => {
        if (events.readyState === EventSource.CLOSED) finish()  // the run is gone; show what was saved
      }
    },
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
      .catch(onError)
    return () => source.current?.close()
  }, [conversationId, load, follow, onError])

  useEffect(() => {
    if (target) return
    const initial = detail?.target ?? savedTarget() ?? firstTarget(providers)
    if (initial) setTarget(initial)
  }, [detail, providers, target])

  useLayoutEffect(() => {
    const element = scroller.current
    if (element && stick.current) element.scrollTop = element.scrollHeight
  })

  function chooseTarget(next: Target) {
    setTarget(next)
    localStorage.setItem(LAST_TARGET, JSON.stringify(next))
  }

  async function send() {
    const text = input.trim()
    if (!text || !target || sending || live) return
    setSending(true)
    try {
      let id = conversationId
      if (!id) id = (await api.create(props.newTemporary)).id
      const started = await api.send(id, { text, provider: target.provider, model: target.model, effort: target.effort })
      setInput('')
      stick.current = true
      if (id !== conversationId) {
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

  const temporary = detail ? detail.temporary : props.newTemporary
  const messages = detail?.messages ?? []
  const noModels = providers.length > 0 && !providers.some((provider) => provider.configured && provider.models.length)

  return (
    <main className="flex min-w-0 flex-1 flex-col">
      <header className="flex items-center gap-2 border-b border-border px-3 py-2">
        <button onClick={props.onOpenMenu} className="rounded-lg p-1.5 text-muted md:hidden" aria-label="会話一覧">
          <Menu className="size-5" aria-hidden />
        </button>
        <h1 className="hidden min-w-0 truncate text-sm font-medium sm:block">{detail?.title || '新しいチャット'}</h1>
        {temporary && (
          <span className="flex shrink-0 items-center gap-1 rounded-full bg-warn/15 px-2 py-0.5 text-xs text-warn" title="アプリには保存します。エージェント側には記録やメモリを残しません">
            <Timer className="size-3" aria-hidden /> 一時
          </span>
        )}
        <div className="ml-auto min-w-0">
          <ModelPicker providers={providers} target={target} onChange={chooseTarget} onRefresh={props.onRefreshProviders} />
        </div>
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
              {noModels ? (
                <p>使えるモデルがありません。.env に API キーかローカルLLMの URL を書いて再起動してください。</p>
              ) : (
                <>
                  <p className="text-lg text-fg">何でも聞いてください</p>
                  <p className="mt-1 text-sm">モデルは送るたびに右上で変えられます。</p>
                  {!conversationId && (
                    <label className="mt-4 inline-flex items-center gap-2 text-sm">
                      <input type="checkbox" checked={props.newTemporary} onChange={(event) => props.onNewTemporaryChange(event.target.checked)} />
                      一時チャットにする
                    </label>
                  )}
                </>
              )}
            </div>
          )}
          {messages.map((message) => (
            <MessageView
              key={message.id}
              message={message}
              providers={providers}
              streaming={live && live.messageId === message.id ? live : undefined}
              onFork={() => fork(message.id)}
            />
          ))}
        </div>
      </div>

      <div className="border-t border-border bg-bg px-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] pt-3">
        <div className="mx-auto flex max-w-3xl items-end gap-2 rounded-2xl border border-border bg-surface p-2 focus-within:border-accent">
          <textarea
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={(event) => {
              // Enter sends on a keyboard; on touch screens Enter is a new line. Never while the IME is composing.
              if (event.key === 'Enter' && !event.shiftKey && !touch && !event.nativeEvent.isComposing) {
                event.preventDefault()
                send()
              }
            }}
            rows={Math.min(8, Math.max(1, input.split('\n').length))}
            placeholder={target ? 'メッセージを入力' : 'モデルを選んでください'}
            className="max-h-60 min-h-[2.25rem] flex-1 resize-none bg-transparent px-2 py-1.5 outline-none"
          />
          {live ? (
            <button onClick={() => api.cancel(live.runId).catch(onError)} className="rounded-xl bg-raised p-2" aria-label="停止" title="停止">
              <Square className="size-5" aria-hidden />
            </button>
          ) : (
            <button
              onClick={send}
              disabled={!input.trim() || !target || sending}
              className="rounded-xl bg-accent p-2 text-accent-fg disabled:opacity-40"
              aria-label="送信"
              title="送信"
            >
              <ArrowUp className="size-5" aria-hidden />
            </button>
          )}
        </div>
      </div>
    </main>
  )
}
