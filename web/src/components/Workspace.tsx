import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, type AppConfig, type Conversation, type Draft, type Executor, type ProjectList } from '../api'
import ChatView from './ChatView'
import Sidebar from './Sidebar'

function idFromHash(): string | null {
  const match = window.location.hash.match(/^#\/c\/([\w-]+)$/)
  return match ? match[1] : null
}

const BLANK: Draft = { temporary: false, mode: 'chat', project: null }

export default function Workspace({ onSignedOut }: { onSignedOut: () => void }) {
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [includeHidden, setIncludeHidden] = useState(false)
  const [selected, setSelected] = useState<string | null>(idFromHash)
  const [executors, setExecutors] = useState<Executor[]>([])
  const [projects, setProjects] = useState<ProjectList | null>(null)
  const [config, setConfig] = useState<AppConfig | null>(null)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [draft, setDraft] = useState<Draft>(BLANK)
  const [error, setError] = useState<string | null>(null)
  const [discovering, setDiscovering] = useState(false)
  const discoveringRef = useRef(false)
  const lastDiscovery = useRef(0)

  const handle = useCallback(
    (error: unknown) => {
      if (error instanceof ApiError && error.status === 401) onSignedOut()
      else setError(error instanceof Error ? error.message : '処理に失敗しました')
    },
    [onSignedOut],
  )

  const refresh = useCallback(() => {
    api.conversations(includeHidden).then(setConversations).catch(handle)
  }, [includeHidden, handle])

  const discover = useCallback((force = false) => {
    if (discoveringRef.current || (!force && Date.now() - lastDiscovery.current < 60_000)) return
    discoveringRef.current = true
    setDiscovering(true)
    api.discoverSessions().then((result) => {
      lastDiscovery.current = Date.now()
      if (result.errors.length) setError(result.errors.join('／'))
      refresh()
    }).catch(handle).finally(() => {
      discoveringRef.current = false
      setDiscovering(false)
    })
  }, [refresh, handle])

  useEffect(() => {
    discover()
    const focus = () => discover()
    window.addEventListener('focus', focus)
    return () => window.removeEventListener('focus', focus)
  }, [discover])

  const loadExecutors = useCallback(
    (force = false) => {
      api.executors(force).then(setExecutors).catch(handle)
    },
    [handle],
  )

  const loadProjects = useCallback(() => {
    api.projects().then(setProjects).catch(handle)
  }, [handle])

  useEffect(refresh, [refresh])
  useEffect(() => {
    api.config().then(setConfig).catch(handle)
  }, [handle])
  useEffect(() => loadExecutors(), [loadExecutors])
  useEffect(() => {
    loadProjects()
    window.addEventListener('focus', loadProjects)  // git state changes outside the app
    return () => window.removeEventListener('focus', loadProjects)
  }, [loadProjects])
  useEffect(() => {
    const onHash = () => setSelected(idFromHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const select = useCallback((id: string | null) => {
    window.location.hash = id ? `#/c/${id}` : ''
    setSelected(id)
    setDrawerOpen(false)
  }, [])

  const startNew = useCallback(
    (next: Draft) => {
      setDraft(next)
      select(null)
    },
    [select],
  )

  return (
    <div className="flex h-full overflow-hidden">
      {error && <div role="alert" className="fixed inset-x-3 top-3 z-[70] mx-auto flex max-w-xl items-start gap-3 rounded-xl border border-danger bg-bg p-3 text-sm shadow-lg">
        <span className="flex-1 whitespace-pre-wrap">{error}</span>
        <button onClick={() => setError(null)} className="shrink-0 text-muted">閉じる</button>
      </div>}
      <Sidebar
        conversations={conversations}
        projects={projects}
        selected={selected}
        includeHidden={includeHidden}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        onToggleHidden={() => setIncludeHidden((value) => !value)}
        onSelect={select}
        onNew={startNew}
        onChanged={refresh}
        onRefresh={() => { setError(null); refresh(); discover(true) }}
        refreshing={discovering}
        onError={handle}
        onSignOut={() => api.logout().finally(onSignedOut)}
      />
      <ChatView
        key={selected ?? `new-${draft.mode}-${draft.project}-${draft.temporary}`}
        conversationId={selected}
        draft={draft}
        onDraftChange={setDraft}
        executors={executors}
        projects={projects}
        config={config}
        onProjectsChanged={loadProjects}
        onRefreshExecutors={() => loadExecutors(true)}
        onOpenMenu={() => setDrawerOpen(true)}
        onSelect={select}
        onChanged={refresh}
        onRunFinished={() => loadExecutors(false)}
        onError={handle}
      />
    </div>
  )
}
