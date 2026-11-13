import { useCallback, useEffect, useState } from 'react'
import { api, ApiError, type Conversation, type Provider } from '../api'
import ChatView from './ChatView'
import Sidebar from './Sidebar'

function idFromHash(): string | null {
  const match = window.location.hash.match(/^#\/c\/([\w-]+)$/)
  return match ? match[1] : null
}

export default function Workspace({ onSignedOut }: { onSignedOut: () => void }) {
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [includeHidden, setIncludeHidden] = useState(false)
  const [selected, setSelected] = useState<string | null>(idFromHash)
  const [providers, setProviders] = useState<Provider[]>([])
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [temporary, setTemporary] = useState(false)

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

  const loadProviders = useCallback(
    (force = false) => {
      api.providers(force).then(setProviders).catch(handle)
    },
    [handle],
  )

  useEffect(refresh, [refresh])
  useEffect(() => loadProviders(), [loadProviders])
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

  return (
    <div className="flex h-full overflow-hidden">
      <Sidebar
        conversations={conversations}
        selected={selected}
        includeHidden={includeHidden}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        onToggleHidden={() => setIncludeHidden((value) => !value)}
        onSelect={select}
        onNew={(isTemporary) => {
          setTemporary(isTemporary)
          select(null)
        }}
        onChanged={refresh}
        onError={handle}
        onSignOut={() => api.logout().finally(onSignedOut)}
      />
      <ChatView
        key={selected ?? 'new'}
        conversationId={selected}
        newTemporary={temporary}
        onNewTemporaryChange={setTemporary}
        providers={providers}
        onRefreshProviders={() => loadProviders(true)}
        onOpenMenu={() => setDrawerOpen(true)}
        onSelect={select}
        onChanged={refresh}
        onError={handle}
      />
    </div>
  )
}
