import { useRef, useState } from 'react'
import { useBackClose } from '../backClose'
import { ChevronDown, Download, Folder, FolderPlus, X } from 'lucide-react'
import { api, ApiError, type ProjectList } from '../api'

function GitState({ git }: { git: ProjectList['projects'][number]['git'] }) {
  if (!git) return <span className="text-muted">gitなし</span>
  return (
    <span className="text-muted">
      {git.branch ?? '—'}
      {git.changes ? ` · 変更${git.changes}` : ''}
    </span>
  )
}

export default function ProjectPicker({ list, value, onChange, onCreated }: {
  list: ProjectList | null
  value: string | null
  onChange: (name: string) => void
  onCreated: (name: string) => void
}) {
  const [open, setOpen] = useState(false)
  const [position, setPosition] = useState({ left: 0, top: 0 })
  const [dialog, setDialog] = useState<'create' | 'clone' | null>(null)
  useBackClose(dialog !== null, () => setDialog(null))
  const button = useRef<HTMLButtonElement>(null)

  function toggle() {
    // The menu is fixed to the viewport so a scrolling header row can't clip it
    const rect = button.current?.getBoundingClientRect()
    if (rect) setPosition({ left: Math.max(8, Math.min(rect.left, window.innerWidth - 296)), top: rect.bottom + 4 })
    setOpen((state) => !state)
  }
  const current = list?.projects.find((project) => project.name === value)

  return (
    <div className="relative shrink-0">
      <button
        ref={button}
        onClick={toggle}
        className={`flex max-w-48 shrink-0 items-center gap-1.5 rounded-lg border px-2 py-1.5 text-sm ${value ? 'border-border' : 'border-warn text-warn'}`}
        aria-label="プロジェクト"
      >
        <Folder className="size-4 shrink-0" aria-hidden />
        <span className="truncate">{value ?? 'プロジェクトを選択'}</span>
        <ChevronDown className="size-3.5 shrink-0" aria-hidden />
      </button>
      {value && !current && list && <span className="sr-only">フォルダが見つかりません</span>}
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} aria-hidden />
          <div style={position} className="fixed z-50 w-72 rounded-lg border border-border bg-bg py-1 text-sm shadow-lg">
            <div className="max-h-72 overflow-y-auto">
              {list?.projects.length === 0 && <p className="px-3 py-2 text-muted">{list.root} にフォルダがありません</p>}
              {list?.projects.map((project) => (
                <button
                  key={project.name}
                  onClick={() => { setOpen(false); onChange(project.name) }}
                  className={`flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-raised ${project.name === value ? 'bg-raised' : ''}`}
                >
                  <Folder className="size-4 shrink-0 text-muted" aria-hidden />
                  <span className="truncate">{project.name}</span>
                  <span className="ml-auto shrink-0 text-xs"><GitState git={project.git} /></span>
                </button>
              ))}
            </div>
            {(list?.can_create || list?.can_clone) && <div className="my-1 border-t border-border" />}
            {list?.can_create && (
              <button onClick={() => { setOpen(false); setDialog('create') }} className="flex w-full items-center gap-2 px-3 py-1.5 hover:bg-raised">
                <FolderPlus className="size-4" aria-hidden /> 新しいフォルダ
              </button>
            )}
            {list?.can_clone && (
              <button onClick={() => { setOpen(false); setDialog('clone') }} className="flex w-full items-center gap-2 px-3 py-1.5 hover:bg-raised">
                <Download className="size-4" aria-hidden /> git clone…
              </button>
            )}
          </div>
        </>
      )}
      {dialog && list && (
        <ProjectDialog kind={dialog} root={list.root} onClose={() => setDialog(null)} onDone={(name) => { setDialog(null); onCreated(name) }} />
      )}
    </div>
  )
}

function ProjectDialog({ kind, root, onClose, onDone }: {
  kind: 'create' | 'clone'
  root: string
  onClose: () => void
  onDone: (name: string) => void
}) {
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [gitInit, setGitInit] = useState(true)
  const [readme, setReadme] = useState(false)
  const [gitignore, setGitignore] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const result = kind === 'create'
        ? await api.createProject({ name, git_init: gitInit, readme, gitignore })
        : await api.cloneProject({ url, name: name.trim() || undefined })
      onDone(result.name)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : '失敗しました')
    } finally {
      setBusy(false)
    }
  }

  const field = 'mt-1 w-full rounded-lg border border-border bg-bg px-3 py-2 outline-none focus:border-accent'
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <form onSubmit={submit} onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => { if (event.key === 'Enter' && (event.nativeEvent.isComposing || event.keyCode === 229)) event.preventDefault() }} className="w-full max-w-sm rounded-2xl border border-border bg-bg p-5">
        <div className="flex items-center justify-between">
          <h2 className="font-semibold">{kind === 'create' ? '新しいフォルダ' : 'git clone'}</h2>
          <button type="button" onClick={onClose} className="rounded p-1 text-muted" aria-label="閉じる"><X className="size-4" aria-hidden /></button>
        </div>
        {kind === 'clone' && (
          <label className="mt-4 block text-sm">
            URL
            <input autoFocus value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://github.com/… か git@github.com:…" className={field} />
          </label>
        )}
        <label className="mt-4 block text-sm">
          名前{kind === 'clone' && <span className="text-muted">（空ならリポジトリ名）</span>}
          <input autoFocus={kind === 'create'} value={name} onChange={(event) => setName(event.target.value)} className={field} />
        </label>
        {kind === 'create' && (
          <div className="mt-3 space-y-1.5 text-sm">
            <label className="flex items-center gap-2"><input type="checkbox" checked={gitInit} onChange={(event) => setGitInit(event.target.checked)} /> git init する</label>
            <label className="flex items-center gap-2"><input type="checkbox" checked={readme} onChange={(event) => setReadme(event.target.checked)} /> README.md を作る</label>
            <label className="flex items-center gap-2"><input type="checkbox" checked={gitignore} onChange={(event) => setGitignore(event.target.checked)} /> .gitignore を作る</label>
          </div>
        )}
        <p className="mt-3 truncate text-xs text-muted">{root}/{kind === 'create' ? name || '…' : name || '（リポジトリ名）'}</p>
        {error && <p className="mt-2 text-sm text-danger">{error}</p>}
        <div className="mt-4 flex justify-end gap-2">
          <button type="button" onClick={onClose} className="rounded-lg px-3 py-2 text-sm text-muted">やめる</button>
          <button type="submit" disabled={busy || (kind === 'create' ? !name.trim() : !url.trim())} className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-accent-fg disabled:opacity-50">
            {busy ? (kind === 'clone' ? 'clone 中…' : '作成中…') : kind === 'create' ? '作成' : 'clone'}
          </button>
        </div>
      </form>
    </div>
  )
}
