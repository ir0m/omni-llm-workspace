import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, type Block, type ConversationDetail, type PermissionAsk, type Usage } from './api'
import { applyUpdate, mergeUsage } from './blocks'
import { notify } from './notify'

type Live = { runId: string; messageId: string; blocks: Block[]; usage: Usage | null; permissions: PermissionAsk[] }

/** Own the event stream and discard replies from loads that belong to a closed or superseded view. */
export function useConversation(id: string | null, callbacks: {
  onChanged: () => void
  onRunFinished: () => void
  onSelect: (id: string | null) => void
  onError: (error: unknown) => void
}) {
  const [detail, setDetail] = useState<ConversationDetail | null>(null)
  const [live, setLive] = useState<Live | null>(null)
  const [reconnecting, setReconnecting] = useState(false)
  const [revision, setRevision] = useState(0)
  const source = useRef<EventSource | null>(null)
  const generation = useRef(0)
  const mounted = useRef(false)
  const latestLoad = useRef(0)
  const latest = useRef(callbacks)
  latest.current = callbacks

  const isActive = useCallback(() => mounted.current, [])
  const load = useCallback(async (conversationId: string) => {
    const version = ++latestLoad.current
    const epoch = generation.current
    const data = await api.conversation(conversationId)
    if (mounted.current && epoch === generation.current && version === latestLoad.current) setDetail(data)
    return data
  }, [])

  const follow = useCallback(function followRun(runId: string, messageId: string) {
    if (!mounted.current) return
    source.current?.close()
    setLive({ runId, messageId, blocks: [], usage: null, permissions: [] })
    setReconnecting(false)
    const epoch = generation.current
    const events = new EventSource(`/api/runs/${runId}/events`)
    source.current = events
    const current = () => mounted.current && generation.current === epoch && source.current === events
    const change = (update: (value: Live) => Live) => {
      if (current()) setLive((value) => value?.runId === runId ? update(value) : value)
    }
    events.onopen = () => { if (current()) setReconnecting(false) }
    events.addEventListener('update', (event) => {
      const update = JSON.parse((event as MessageEvent).data)
      change((value) => update.sessionUpdate === 'usage_update'
        ? { ...value, usage: mergeUsage(value.usage, update) }
        : { ...value, blocks: applyUpdate(value.blocks, update) })
    })
    events.addEventListener('permission', (event) => {
      if (!current()) return
      const ask: PermissionAsk = JSON.parse((event as MessageEvent).data)
      change((value) => ({ ...value, permissions: [...value.permissions.filter((item) => item.requestId !== ask.requestId), ask] }))
      notify('承認が必要です', ask.command ?? (ask.paths.join(', ') || ask.title))
    })
    events.addEventListener('permission_resolved', (event) => {
      const { requestId } = JSON.parse((event as MessageEvent).data)
      change((value) => ({ ...value, permissions: value.permissions.filter((item) => item.requestId !== requestId) }))
    })
    events.addEventListener('input_changed', () => {
      if (current() && id) load(id).catch(latest.current.onError)
    })
    const finish = async () => {
      if (!current()) return
      events.close()
      source.current = null
      setReconnecting(false)
      try {
        const data = id ? await load(id) : null
        if (!mounted.current || generation.current !== epoch || source.current) return
        const streaming = data?.messages.findLast((message) => message.status === 'streaming')
        if (data?.active_run && streaming) followRun(data.active_run, streaming.id)
        else setLive(null)
      } catch (error) {
        if (!mounted.current || generation.current !== epoch) return
        // A failed final reload must not leave the composer permanently locked.
        setLive(null)
        latest.current.onError(error)
      }
      if (!mounted.current || generation.current !== epoch) return
      latest.current.onChanged()
      latest.current.onRunFinished()
      setRevision((value) => value + 1)
    }
    events.addEventListener('status', (event) => {
      if (!current()) return
      const status = JSON.parse((event as MessageEvent).data).status
      if (status !== 'running') {
        notify(status === 'complete' ? '応答が完了しました' : '応答が止まりました', document.title)
        void finish()
      }
    })
    events.onerror = () => {
      if (!current()) return
      if (events.readyState === EventSource.CLOSED) void finish()
      else setReconnecting(true) // EventSource resumes automatically with Last-Event-ID.
    }
  }, [id, load])

  useEffect(() => {
    mounted.current = true
    const epoch = ++generation.current
    if (id) {
      load(id).then((data) => {
        if (!mounted.current || generation.current !== epoch) return
        const streaming = data.messages.findLast((message) => message.status === 'streaming')
        if (data.active_run && streaming) follow(data.active_run, streaming.id)
      }).catch((error) => {
        if (!mounted.current || generation.current !== epoch) return
        if (error instanceof ApiError && error.status === 404) latest.current.onSelect(null)
        latest.current.onError(error)
      })
    }
    return () => {
      mounted.current = false
      generation.current += 1
      source.current?.close()
      source.current = null
    }
  }, [id, follow, load])

  return { detail, setDetail, live, reconnecting, revision, load, follow, isActive }
}
