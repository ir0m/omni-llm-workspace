import { useState } from 'react'
import {
  ArrowDown, ArrowUp, ChevronDown, ChevronRight, Eye, EyeOff, FolderMinus, GitBranch, KeyRound, LogOut, MoreHorizontal, Pencil,
  Pin, PinOff, Plus, RefreshCw, Search, Timer, Trash2, X,
} from 'lucide-react'
import { api, type Conversation, type Draft, type ProjectList } from '../api'
import PasskeyDialog from './PasskeyDialog'

const COLLAPSED = 'omni.collapsedGroups'

type Group = {
  key: string
  label: string
  items: Conversation[]
  project?: string
  detail?: string
  missing?: boolean
}

function loadCollapsed(): Set<string> {
  try {
    return new Set(JSON.parse(localStorage.getItem(COLLAPSED) ?? '[]'))
  } catch {
    return new Set()
  }
}

/** 📌 pinned first, then chats without a project, then one group per project (most recently active first). */
function groupConversations(conversations: Conversation[], projects: ProjectList | null): Group[] {
  const groups: Group[] = [
    { key: 'pinned', label: '📌 ピン留め', items: conversations.filter((item) => item.pinned) },
    { key: 'chat', label: 'チャット', items: conversations.filter((item) => !item.pinned && !item.project) },
  ]
  const known = new Map((projects?.projects ?? []).map((project) => [project.name, project]))
  const names = new Set([...known.keys(), ...conversations.map((item) => item.project).filter((name): name is string => !!name)])
  const latest = (name: string) => Math.max(0, ...conversations.filter((item) => item.project === name).map((item) => item.updated_at))
  const ordered = [...names].sort((a, b) => latest(b) - latest(a) || a.localeCompare(b))
  for (const name of ordered) {
    const git = known.get(name)?.git
    groups.push({
      key: `project:${name}`,
      label: name,
      project: name,
      missing: !known.has(name),
      detail: !known.has(name) ? 'フォルダなし' : git ? `${git.branch ?? '—'}${git.changes ? ` · 変更${git.changes}` : ''}` : 'gitなし',
      items: conversations.filter((item) => !item.pinned && item.project === name),
    })
  }
  return groups
}

type Props = {
  conversations: Conversation[]
  projects: ProjectList | null
  selected: string | null
  includeHidden: boolean
  open: boolean
  onClose: () => void
  onToggleHidden: () => void
  onSelect: (id: string | null) => void
  onNew: (draft: Draft) => void
  onChanged: () => void
  onRefresh: () => void
  refreshing: boolean
  onError: (error: unknown) => void
  onSignOut: () => void
}

export default function Sidebar(props: Props) {
  const { selected, open, onClose } = props
  const [query, setQuery] = useState('')
  const [dragging, setDragging] = useState<{ id: string; group: string } | null>(null)
  const [collapsed, setCollapsed] = useState(loadCollapsed)
  const [passkeys, setPasskeys] = useState(false)
  const search = query.trim().toLowerCase()
  const shown = props.conversations.filter((item) => item.title.toLowerCase().includes(search))
  const groups = groupConversations(shown, props.projects).filter((group) =>
    search ? group.items.length > 0 : group.key !== 'pinned' || group.items.length > 0)

  function toggle(key: string) {
    const next = new Set(collapsed)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    setCollapsed(next)
    localStorage.setItem(COLLAPSED, JSON.stringify([...next]))
  }

  function move(group: Group, id: string, to: number) {
    const order = group.items.map((item) => item.id).filter((item) => item !== id)
    order.splice(Math.max(0, Math.min(order.length, to)), 0, id)
    api.reorder(order).then(props.onChanged).catch(props.onError)
  }

  return (
    <>
      {open && <div className="fixed inset-0 z-20 bg-black/40 md:hidden" onClick={onClose} aria-hidden />}
      <aside
        className={`fixed inset-y-0 left-0 z-30 flex w-72 flex-col border-r border-border bg-surface transition-transform md:static md:translate-x-0 ${
          open ? 'translate-x-0' : '-translate-x-full'
        }`}
      >
        <div className="flex items-center gap-2 p-3">
          <button
            onClick={() => props.onNew({ temporary: false, mode: 'chat', project: null })}
            className="flex flex-1 items-center justify-center gap-1.5 rounded-lg bg-accent px-3 py-2 text-sm font-medium text-accent-fg"
          >
            <Plus className="size-4" aria-hidden /> 新しいチャット
          </button>
          <button
            onClick={() => props.onNew({ temporary: true, mode: 'chat', project: null })}
            title="一時チャット: アプリには保存して後から開けます。エージェント側には記録やメモリを残しません"
            className="rounded-lg border border-border p-2 text-muted hover:text-fg"
            aria-label="新しい一時チャット"
          >
            <Timer className="size-4" aria-hidden />
          </button>
          <button onClick={onClose} className="rounded-lg p-2 text-muted md:hidden" aria-label="閉じる">
            <X className="size-4" aria-hidden />
          </button>
        </div>
        <div className="flex items-center gap-2 px-3 pb-2">
          <label className="flex min-w-0 flex-1 items-center gap-2 rounded-lg border border-border bg-bg px-2 py-1.5">
            <Search className="size-4 text-muted" aria-hidden />
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="検索" className="w-full bg-transparent text-sm outline-none" />
          </label>
          <button onClick={props.onRefresh} disabled={props.refreshing} aria-label="会話一覧を更新" title="会話一覧を更新"
            className="rounded-lg p-2 text-muted hover:text-fg disabled:opacity-50">
            <RefreshCw className={`size-4 ${props.refreshing ? 'animate-spin' : ''}`} aria-hidden />
          </button>
        </div>
        <nav className="flex-1 overflow-y-auto px-2 pb-2">
          {groups.map((group) => {
            const isCollapsed = !search && collapsed.has(group.key)
            return (
              <section key={group.key} className="mb-1">
                <div className="group/header flex items-center rounded-lg pr-1 text-xs text-muted hover:bg-raised/40">
                  <button onClick={() => toggle(group.key)} className="flex min-w-0 flex-1 items-center gap-1 px-1.5 py-1.5 text-left">
                    {isCollapsed ? <ChevronRight className="size-3.5 shrink-0" aria-hidden /> : <ChevronDown className="size-3.5 shrink-0" aria-hidden />}
                    <span className={`truncate font-medium ${group.missing ? 'line-through' : 'text-fg/80'}`}>{group.label}</span>
                    {group.detail && <span className="shrink-0 truncate">· {group.detail}</span>}
                    <span className="ml-auto shrink-0 pl-1">{group.items.length}</span>
                  </button>
                  {group.key !== 'pinned' && !group.missing && (
                    <button
                      onClick={() => props.onNew(group.project
                        ? { temporary: false, mode: 'code', project: group.project }
                        : { temporary: false, mode: 'chat', project: null })}
                      className="rounded p-1 hover:text-fg"
                      title={group.project ? `${group.project} で新しいCode` : '新しいチャット'}
                      aria-label={group.project ? `${group.project} で新しいCode` : '新しいチャット'}
                    >
                      <Plus className="size-3.5" aria-hidden />
                    </button>
                  )}
                </div>
                {!isCollapsed && group.items.map((item, index) => (
                  <Item
                    key={item.id}
                    item={item}
                    active={item.id === selected}
                    showProject={group.key === 'pinned'}
                    onSelect={() => props.onSelect(item.id)}
                    draggable={!search}
                    onDragStart={() => setDragging({ id: item.id, group: group.key })}
                    onDrop={() => {
                      if (dragging && dragging.group === group.key && dragging.id !== item.id) move(group, dragging.id, index)
                      setDragging(null)
                    }}
                    onMove={(delta) => move(group, item.id, index + delta)}
                    onChanged={props.onChanged}
                    onError={props.onError}
                    onForked={(id) => {
                      props.onChanged()
                      props.onSelect(id)
                    }}
                    onDeleted={() => {
                      props.onChanged()
                      if (item.id === selected) props.onSelect(null)
                    }}
                  />
                ))}
              </section>
            )
          })}
          {props.conversations.length === 0 && <p className="px-2 py-6 text-center text-sm text-muted">会話はまだありません</p>}
        </nav>
        <div className="flex items-center justify-between border-t border-border p-2 text-sm">
          <button onClick={props.onToggleHidden} className="flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-muted hover:text-fg">
            {props.includeHidden ? <EyeOff className="size-4" aria-hidden /> : <Eye className="size-4" aria-hidden />}
            {props.includeHidden ? '非表示の会話を隠す' : '非表示の会話も表示'}
          </button>
          <span className="flex">
            <button onClick={() => setPasskeys(true)} className="rounded-lg p-2 text-muted hover:text-fg" aria-label="パスキー" title="パスキー">
              <KeyRound className="size-4" aria-hidden />
            </button>
            <button onClick={props.onSignOut} className="rounded-lg p-2 text-muted hover:text-fg" aria-label="ログアウト" title="ログアウト">
              <LogOut className="size-4" aria-hidden />
            </button>
          </span>
        </div>
      </aside>
      {passkeys && <PasskeyDialog onClose={() => setPasskeys(false)} onSignedOut={props.onSignOut} />}
    </>
  )
}

type ItemProps = {
  item: Conversation
  active: boolean
  draggable: boolean
  showProject: boolean
  onSelect: () => void
  onDragStart: () => void
  onDrop: () => void
  onMove: (delta: number) => void
  onChanged: () => void
  onError: (error: unknown) => void
  onForked: (id: string) => void
  onDeleted: () => void
}

function Item({ item, active, draggable, showProject, ...on }: ItemProps) {
  const [menu, setMenu] = useState(false)
  const [renaming, setRenaming] = useState(false)
  const [title, setTitle] = useState(item.title)

  function act(promise: Promise<unknown>, after: () => void = on.onChanged) {
    setMenu(false)
    promise.then(after).catch(on.onError)
  }

  function rename() {
    setRenaming(false)
    if (title.trim() && title.trim() !== item.title) act(api.update(item.id, { title: title.trim() }))
  }

  return (
    <div
      draggable={draggable && !renaming}
      onDragStart={on.onDragStart}
      onDragOver={(event) => event.preventDefault()}
      onDrop={on.onDrop}
      className={`group relative mb-0.5 ml-3 flex items-center rounded-lg ${active ? 'bg-raised' : 'hover:bg-raised/60'} ${item.hidden ? 'opacity-60' : ''}`}
    >
      {renaming ? (
        <input
          autoFocus
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          onBlur={rename}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.nativeEvent.isComposing) rename()
            if (event.key === 'Escape') setRenaming(false)
          }}
          className="m-1 w-full rounded border border-accent bg-bg px-2 py-1 text-sm outline-none"
        />
      ) : (
        <button onClick={on.onSelect} className="flex min-w-0 flex-1 items-center gap-1.5 px-2 py-1.5 text-left text-sm">
          {item.temporary && <Timer className="size-3.5 shrink-0 text-warn" aria-label="一時" />}
          <span className="truncate">{item.title || '無題'}</span>
          {showProject && item.project && <span className="shrink-0 truncate text-xs text-muted">◇{item.project}</span>}
          {item.mode === 'code' && !showProject && <span className="shrink-0 rounded bg-raised px-1 text-[10px] text-muted">Code</span>}
          {item.active_run && <span className="ml-auto size-2 shrink-0 animate-pulse rounded-full bg-accent" aria-label="応答中" />}
        </button>
      )}
      {!renaming && (
        <button
          onClick={() => setMenu((value) => !value)}
          className={`mr-1 rounded p-1 text-muted hover:text-fg ${menu ? '' : 'md:opacity-0 md:group-hover:opacity-100'}`}
          aria-label="操作"
        >
          <MoreHorizontal className="size-4" aria-hidden />
        </button>
      )}
      {menu && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setMenu(false)} aria-hidden />
          <div className="absolute right-1 top-9 z-50 w-48 rounded-lg border border-border bg-bg py-1 text-sm shadow-lg">
            <MenuItem icon={Pencil} label="名前を変更" onClick={() => { setMenu(false); setTitle(item.title); setRenaming(true) }} />
            <MenuItem icon={item.pinned ? PinOff : Pin} label={item.pinned ? 'ピン留めを外す' : 'ピン留め'} onClick={() => act(api.update(item.id, { pinned: !item.pinned }))} />
            <MenuItem icon={ArrowUp} label="上へ移動" onClick={() => { setMenu(false); on.onMove(-1) }} />
            <MenuItem icon={ArrowDown} label="下へ移動" onClick={() => { setMenu(false); on.onMove(1) }} />
            <MenuItem icon={item.hidden ? Eye : EyeOff} label={item.hidden ? '表示する' : '非表示にする'} onClick={() => act(api.update(item.id, { hidden: !item.hidden }))} />
            {item.project && (
              <MenuItem icon={FolderMinus} label="プロジェクトから外す" onClick={() => act(api.update(item.id, { project: '' }))} />
            )}
            <MenuItem
              icon={GitBranch}
              label="分岐"
              onClick={() => {
                setMenu(false)
                api.fork(item.id).then((copy) => on.onForked(copy.id)).catch(on.onError)
              }}
            />
            <MenuItem
              icon={Trash2}
              label="削除"
              danger
              onClick={() => {
                if (window.confirm(`「${item.title || '無題'}」を削除しますか？元に戻せません`)) act(api.remove(item.id), on.onDeleted)
                else setMenu(false)
              }}
            />
          </div>
        </>
      )}
    </div>
  )
}

function MenuItem({ icon: Icon, label, onClick, danger }: {
  icon: React.ComponentType<{ className?: string }>
  label: string
  onClick: () => void
  danger?: boolean
}) {
  return (
    <button onClick={onClick} className={`flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-raised ${danger ? 'text-danger' : ''}`}>
      <Icon className="size-4" />
      {label}
    </button>
  )
}
