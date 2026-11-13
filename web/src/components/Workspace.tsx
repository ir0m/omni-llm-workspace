import { useCallback, useEffect, useState } from 'react'
import { api, ApiError, type Conversation, type Draft, type Executor, type ProjectList } from '../api'
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
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [draft, setDraft] = useState<Draft>(BLANK)

  const handle = useCallback(
    (error: unknown) => {
      if (error instanceof ApiError && error.status === 401) onSignedOut()
      else console.error(error)
    },
    [onSignedOut],
  )

  const refresh = useCallback(() => {
    api.conversations(includeHidden).then(setConversations).catch(handle)
  }, [includeHidden, handle])

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
        onProjectsChanged={loadProjects}
        onRefreshExecutors={() => loadExecutors(true)}
        onOpenMenu={() => setDrawerOpen(true)}
        onSelect={select}
        onChanged={refresh}
        onError={handle}
      />
    </div>
  )
}
