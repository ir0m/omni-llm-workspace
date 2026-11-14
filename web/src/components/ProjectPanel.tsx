import { useEffect, useRef, useState } from 'react'
import { ChevronDown, ChevronRight, File, Folder, Link, RefreshCw, X } from 'lucide-react'
import { lazy, Suspense } from 'react'
import WorktreePanel from './WorktreePanel'
const TerminalPanel = lazy(() => import('./TerminalPanel'))
import { api, type ChangeStage, type FileDiff, type FilePreview, type FileTree, type ProjectChanges } from '../api'

const STAGES: { key: ChangeStage; label: string }[] = [
  { key: 'unstaged', label: '未ステージ' }, { key: 'staged', label: 'ステージ済み' }, { key: 'untracked', label: '未追跡' },
]
const STATUS: Record<string, string> = { A: '追加', M: '変更', D: '削除', R: '改名', C: 'コピー', U: '競合', T: '種類変更', '?': '未追跡' }
const failure = (error: unknown) => error instanceof Error ? error.message : '読み込めませんでした'
const sizeLabel = (size: number) => size >= 1024 * 1024 ? `${(size / 1024 / 1024).toFixed(1)} MiB`
  : size >= 1024 ? `${(size / 1024).toFixed(1)} KiB` : `${size} B`

type TreeProps = { project: string; path: string; depth: number; version: number; selected: string | null; onSelect: (path: string) => void }

export function Tree(props: TreeProps) {
  const { project, path, version, depth, selected, onSelect } = props
  const [tree, setTree] = useState<FileTree | null>(null)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [error, setError] = useState<string | null>(null)
  const [more, setMore] = useState(false)
  const generation = useRef(0)
  useEffect(() => {
    const current = ++generation.current
    setError(null)
    setMore(false)
    api.files(project, path).then((data) => {
      if (current === generation.current) setTree(data)
    }).catch((reason) => {
      if (current === generation.current) setError(failure(reason))
    })
    return () => { generation.current++ }
  }, [project, path, version])

  async function loadMore() {
    if (tree?.next_offset == null || more) return
    const current = generation.current
    setMore(true)
    try {
      const next = await api.files(project, path, tree.next_offset)
      if (current === generation.current) setTree({ ...next, entries: [...tree.entries, ...next.entries] })
    } catch (reason) {
      if (current === generation.current) setError(failure(reason))
    } finally {
      if (current === generation.current) setMore(false)
    }
  }

  return <>
    {error && <p role="alert" className="px-3 py-2 text-sm text-danger">{error}</p>}
    {!tree && !error && <p className="px-3 py-2 text-sm text-muted">読み込み中…</p>}
    {tree && <ul>
      {tree.entries.map((entry) => {
        const folder = entry.kind === 'directory'
        const open = expanded.has(entry.path)
        const unavailable = !folder && entry.kind !== 'file'
        return <li key={entry.path}>
          <button disabled={unavailable} aria-expanded={folder ? open : undefined}
            aria-label={entry.name} aria-current={!folder && selected === entry.path ? 'true' : undefined}
            title={unavailable ? `${entry.name}（リンク・特殊ファイルは閲覧できません）` : entry.path}
            onClick={() => {
              if (!folder) onSelect(entry.path)
              else setExpanded((old) => {
                const next = new Set(old)
                if (next.has(entry.path)) next.delete(entry.path)
                else next.add(entry.path)
                return next
              })
            }}
            style={{ paddingLeft: 12 + Math.min(depth * 14, 98) }}
            className={`flex w-full items-center gap-1.5 py-1.5 pr-3 text-left text-sm disabled:opacity-45 ${selected === entry.path ? 'bg-raised' : 'hover:bg-raised/60'}`}>
            {folder ? open ? <ChevronDown className="size-3.5 shrink-0" /> : <ChevronRight className="size-3.5 shrink-0" /> : <span className="w-3.5 shrink-0" />}
            {folder ? <Folder className="size-4 shrink-0 text-muted" /> : entry.kind === 'link' ? <Link className="size-4 shrink-0 text-muted" /> : <File className="size-4 shrink-0 text-muted" />}
            <span className="truncate">{entry.name}</span>
          </button>
          {folder && open && <Tree {...props} path={entry.path} depth={depth + 1} />}
        </li>
      })}
      {tree.entries.length === 0 && <li className="px-3 py-2 text-sm text-muted">表示できるファイルはありません</li>}
    </ul>}
    {tree?.next_offset != null && <button onClick={loadMore} disabled={more} className="m-2 rounded-lg border border-border px-3 py-1 text-sm disabled:opacity-50">
      {more ? '読み込み中…' : 'さらに表示'}
    </button>}
  </>
}

export default function ProjectPanel({ project, revision, capabilities, onChanged, onClose }: { project: string; revision: number; capabilities: string[]; onChanged: () => void; onClose: () => void }) {
  const [tab, setTab] = useState<'files' | 'diff' | 'terminal' | 'worktrees'>('files')
  const [refresh, setRefresh] = useState(0)
  const version = revision + refresh
  const [filePath, setFilePath] = useState<string | null>(null)
  const [change, setChange] = useState<{ path: string; stage: ChangeStage } | null>(null)
  const [changes, setChanges] = useState<ProjectChanges | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [preview, setPreview] = useState<FilePreview | null>(null)
  const [diff, setDiff] = useState<FileDiff | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [mobile, setMobile] = useState(() => !window.matchMedia('(min-width: 1024px)').matches)
  const panel = useRef<HTMLElement>(null)
  const close = useRef<HTMLButtonElement>(null)
  const tabs: ('files' | 'diff' | 'terminal' | 'worktrees')[] = ['files', 'diff']
  if (capabilities.includes('terminal')) tabs.push('terminal')
  if (capabilities.includes('worktrees')) tabs.push('worktrees')
  const selected = tab === 'files' ? filePath : tab === 'diff' ? change?.path : null

  useEffect(() => {
    const query = window.matchMedia('(min-width: 1024px)')
    const update = () => setMobile(!query.matches)
    query.addEventListener('change', update)
    return () => query.removeEventListener('change', update)
  }, [])

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    close.current?.focus()
    return () => { if (previous?.isConnected) previous.focus() }
  }, [])

  useEffect(() => {
    if (mobile && !panel.current?.contains(document.activeElement)) close.current?.focus()
  }, [mobile])

  useEffect(() => {
    if (tab !== 'diff') return
    let cancelled = false
    setChanges(null)
    setListError(null)
    api.changes(project).then((data) => { if (!cancelled) setChanges(data) })
      .catch((reason) => { if (!cancelled) setListError(failure(reason)) })
    return () => { cancelled = true }
  }, [tab, project, version])

  useEffect(() => {
    let cancelled = false
    setPreview(null)
    setDiff(null)
    setError(null)
    if (!selected) { setLoading(false); return }
    setLoading(true)
    const request = tab === 'files'
      ? api.file(project, selected).then((data) => { if (!cancelled) setPreview(data) })
      : api.diff(project, selected, change!.stage).then((data) => { if (!cancelled) setDiff(data) })
    request.catch((reason) => { if (!cancelled) setError(failure(reason)) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [tab, selected, change?.stage, project, version])

  return <section ref={panel} role={mobile ? 'dialog' : 'complementary'} aria-modal={mobile ? true : undefined}
    aria-label={`${project} のファイル・差分`}
    onKeyDown={(event) => {
      if (event.key === 'Escape') { event.stopPropagation(); onClose() }
      if (mobile && event.key === 'Tab') {
        const elements = Array.from(panel.current?.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex="0"]') ?? [])
          .filter((element) => element.getClientRects().length > 0)
        const first = elements[0], last = elements[elements.length - 1]
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
      }
    }}
    className="fixed inset-0 z-40 flex min-h-0 min-w-0 flex-col bg-bg lg:static lg:z-auto lg:w-[min(40vw,36rem)] lg:shrink-0 lg:border-l lg:border-border">
    <div className="flex items-center gap-2 border-b border-border px-3 py-2 pt-[max(0.5rem,env(safe-area-inset-top))]">
      <span className="min-w-0 flex-1 truncate text-sm font-medium">{project}</span>
      <button onClick={() => setRefresh((value) => value + 1)} aria-label="ファイルと差分を更新" title="ファイルと差分を更新" className="rounded-lg p-2 text-muted hover:text-fg">
        <RefreshCw className="size-4" aria-hidden />
      </button>
      <button ref={close} onClick={onClose} aria-label="ファイル・差分を閉じる" className="flex items-center gap-1 rounded-lg p-2 text-sm text-muted hover:text-fg">
        <X className="size-4" aria-hidden /><span className="lg:hidden">閉じる</span>
      </button>
    </div>
    <div className="flex border-b border-border" role="tablist" aria-label="プロジェクトの表示"
      onKeyDown={(event) => {
        if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
          event.preventDefault()
          const next = event.key === 'Home' ? tabs[0] : event.key === 'End' ? tabs[tabs.length - 1] : tabs[(tabs.findIndex((t) => t === tab) + (event.key === 'ArrowLeft' ? tabs.length - 1 : 1)) % tabs.length]
          setTab(next)
          event.currentTarget.querySelector<HTMLButtonElement>(`[data-tab="${next}"]`)?.focus()
        }
      }}>
      {tabs.map((value) => <button key={value} role="tab" data-tab={value}
        id={`project-tab-${value}`} aria-controls="project-tab-content" aria-selected={tab === value} tabIndex={tab === value ? 0 : -1}
        onClick={() => setTab(value)} className={`flex-1 border-b-2 px-3 py-2 text-sm ${tab === value ? 'border-accent text-accent' : 'border-transparent text-muted'}`}>
        {value === 'files' ? 'ファイル' : value === 'diff' ? '差分' : value === 'terminal' ? '端末' : '並列作業'}
      </button>)}
    </div>
    <div id="project-tab-content" role="tabpanel" aria-labelledby={`project-tab-${tab}`} className="flex min-h-0 flex-1 flex-col">
      {tab === 'terminal' ? <Suspense fallback={<p className="p-3">読み込み中…</p>}><TerminalPanel project={project} /></Suspense> : tab === 'worktrees' ? <WorktreePanel project={project} onChanged={onChanged} /> : <>
      <div className="max-h-[38%] min-h-24 shrink-0 overflow-y-auto border-b border-border py-1" aria-label={tab === 'files' ? 'ファイル一覧' : '変更ファイル一覧'}>
        {tab === 'files' ? <Tree project={project} path="" depth={0} version={version} selected={filePath} onSelect={setFilePath} /> : <>
          {listError && <p role="alert" className="p-3 text-sm text-danger">{listError}</p>}
          {!changes && !listError && <p className="p-3 text-sm text-muted">読み込み中…</p>}
          {changes && !changes.is_git && <p className="p-3 text-sm text-muted">このプロジェクトはGitで管理されていません</p>}
          {changes?.is_git && STAGES.map(({ key, label }) => <div key={key}>
            <h3 className="px-3 py-1.5 text-xs font-medium text-muted">{label}（{changes[key].length}）</h3>
            {changes[key].map((item) => <button key={item.path} title={item.old_path ? `${item.old_path} → ${item.path}` : item.path}
              onClick={() => setChange({ path: item.path, stage: key })}
              className={`flex w-full items-start gap-2 px-3 py-1.5 text-left text-sm ${change?.path === item.path && change.stage === key ? 'bg-raised' : 'hover:bg-raised/60'}`}>
              <span className="min-w-0 flex-1 break-all">{item.path}</span>
              <span className="shrink-0 text-xs text-muted">{STATUS[item.status] ?? item.status}</span>
            </button>)}
          </div>)}
          {changes?.is_git && STAGES.every(({ key }) => !changes[key].length) && <p className="p-3 text-sm text-muted">変更はありません</p>}
        </>}
      </div>
      {selected && <div className="flex items-start gap-2 border-b border-border px-3 py-2 text-xs">
        <span className="min-w-0 flex-1 break-all">{selected}</span>
        {preview && <span className="shrink-0 text-muted">{sizeLabel(preview.size)}</span>}
      </div>}
      <div className="min-h-0 flex-1 overflow-auto pb-[env(safe-area-inset-bottom)]" tabIndex={0} aria-label="ファイルの内容">
        {!selected && <p className="p-4 text-sm text-muted">{tab === 'files' ? 'ファイルを選択すると内容を表示します' : '変更ファイルを選択すると差分を表示します'}</p>}
        {loading && <p className="p-4 text-sm text-muted">読み込み中…</p>}
        {error && <p role="alert" className="p-4 text-sm text-danger">{error}</p>}
        {preview?.kind === 'text' && <>
          {preview.truncated && <p className="p-3 text-xs text-muted">先頭256 KiBを表示しています</p>}
          <pre className="min-w-full w-max p-3 font-mono text-xs leading-5"><code>{preview.text || '（空のファイル）'}</code></pre>
        </>}
        {preview?.kind === 'image' && (preview.too_large
          ? <p className="p-4 text-sm text-muted">画像は8 MiBまで表示できます</p>
          : <img key={`${preview.path}-${version}`} src={api.imageUrl(project, preview.path, version)} alt={preview.path}
              onError={() => setError('画像を読み込めませんでした。更新して再度お試しください')}
              className="mx-auto max-w-full p-3" />)}
        {preview?.kind === 'pdf' && <p className="p-4 text-sm text-muted">PDFはDocsで表示できます</p>}
        {preview?.kind === 'binary' && <p className="p-4 text-sm text-muted">この形式はプレビューできません</p>}
        {diff && <>
          {diff.truncated && <p className="p-3 text-xs text-muted">差分が大きいため先頭部分を表示しています</p>}
          <pre className="min-w-full w-max py-3 font-mono text-xs leading-5"><code>{diff.patch ? diff.patch.split('\n').map((line, index) =>
            <span key={index} className={`block px-3 ${line.startsWith('@@') ? 'bg-accent/10 text-accent'
              : line.startsWith('+++') || line.startsWith('---') ? 'text-muted'
              : line.startsWith('+') ? 'bg-ok/10 text-ok' : line.startsWith('-') ? 'bg-danger/10 text-danger' : ''}`}>
              {line || '\u00a0'}
            </span>) : <span className="px-3 text-muted">差分はありません</span>}</code></pre>
        </>}
      </div>
      </>}
    </div>
  </section>
}
