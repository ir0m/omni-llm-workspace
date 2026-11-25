import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Columns2, Eye, Play, Presentation, FilePlus, FolderPlus, FolderTree, Menu, Pencil, RefreshCw, Save, SquarePen, Trash2 } from 'lucide-react'
import { api, ApiError, type CompileResult, type NoteIndex, type ProjectList } from '../api'
import NoteMarkdown from './NoteMarkdown'
import { Tree } from './ProjectPanel'
import { isMarp, type DeckSpec } from '../deckSpec'
import { PREVIEW, Resizer, TREE, useWidth, widthVar } from '../resize'

const Editor = lazy(() => import('./Editor'))
const PdfView = lazy(() => import('./PdfView'))
const Presenter = lazy(() => import('./Presenter'))
const MarpPreview = lazy(() => import('./MarpPreview'))
const ImageView = lazy(() => import('./ImageView'))

const SAVED = 'omni.docs'
type View = 'edit' | 'split' | 'preview'
type Loaded = { path: string; text: string; hash: string | null; truncated: boolean; serial: number }
type Other = { path: string; kind: 'image' | 'pdf' | 'binary'; tooLarge: boolean }
type Form = { kind: 'file' | 'folder' | 'rename'; value: string }

function remembered(): { project?: string; path?: string; showHidden?: boolean } {
  try {
    const value = JSON.parse(localStorage.getItem(SAVED) ?? '{}')
    return value && typeof value === 'object' ? value : {}
  } catch {
    return {}
  }
}

const isCompiled = (path: string) => /\.(typ|tex)$/i.test(path)
const isMarkdown = (path: string) => /\.(md|markdown|mdx)$/i.test(path)
const isSvg = (path: string) => /\.svg$/i.test(path)
const folderOf = (path: string) => path.includes('/') ? path.slice(0, path.lastIndexOf('/') + 1) : ''
const inside = (path: string, folder: string) => path.startsWith(`${folder}/`)
const sizeText = (size: number) => size >= 1024 * 1024 ? `${(size / 1024 / 1024).toFixed(1)} MiB` : size >= 1024 ? `${(size / 1024).toFixed(1)} KiB` : `${size} B`

export default function DocsView({ projects, canEdit, canCompile, onOpenMenu, sidebarHidden = false, sideWidth = 0, onError }: {
  projects: ProjectList | null; canEdit: boolean; canCompile: boolean
  onOpenMenu: () => void; sidebarHidden?: boolean; sideWidth?: number; onError: (error: unknown) => void
}) {
  const [project, setProject] = useState<string | null>(() => remembered().project ?? null)
  const [path, setPath] = useState<string | null>(() => remembered().path ?? null)
  const [showHidden, setShowHidden] = useState(() => remembered().showHidden === true)
  const [loaded, setLoaded] = useState<Loaded | null>(null)
  const [other, setOther] = useState<Other | null>(null)
  const [loading, setLoading] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const [conflict, setConflict] = useState(false)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [preview, setPreview] = useState('')
  const [view, setView] = useState<View>('split')
  const [treeOpen, setTreeOpen] = useState(true)
  const tree = useWidth('tree', TREE, sideWidth)
  // In split view the preview has the chosen width and the editor takes the rest
  const previewSize = useWidth('preview', PREVIEW, sideWidth + (treeOpen ? tree.width : 0))
  const [version, setVersion] = useState(0)
  const [noteIndex, setNoteIndex] = useState<NoteIndex>({ notes: [], paths: [], backlinks: [], results: [], truncated: false })
  const [query, setQuery] = useState('')
  const [indexError, setIndexError] = useState<string | null>(null)
  const [fragment, setFragment] = useState('')
  const [form, setForm] = useState<Form | null>(null)
  // The folder last clicked in the list, for rename and delete; cleared when a file is opened
  const [folder, setFolder] = useState<string | null>(null)
  const [built, setBuilt] = useState<{ key: string; result: CompileResult } | null>(null)
  const [building, setBuilding] = useState(false)
  const [buildError, setBuildError] = useState<string | null>(null)
  const [pdfVersion, setPdfVersion] = useState(0)
  const [logOpen, setLogOpen] = useState(false)
  const [jump, setJump] = useState<{ line: number; tick: number } | null>(null)
  const buildSerial = useRef(0)
  const [presenting, setPresenting] = useState<DeckSpec | null>(null)
  const text = useRef('')
  const serial = useRef(0)
  const previewTimer = useRef<number | undefined>(undefined)

  const names = projects?.projects.map((item) => item.name) ?? []
  const known = project !== null && names.includes(project)
  const editable = canEdit && !!loaded && !loaded.truncated && loaded.hash !== null
  const listed = (file: { path: string }) => showHidden || !file.path.split('/').some((part) => part.startsWith('.'))
  const searchResults = noteIndex.results.filter(listed)
  const backlinks = noteIndex.backlinks.filter(listed)

  useEffect(() => {
    let active = true
    const refresh = () => {
      if (!project || !known) return
      api.notes(project, path ?? '', query).then((data) => {
        if (active) { setNoteIndex(data); setIndexError(null) }
      }).catch((error) => { if (active) setIndexError(error instanceof Error ? error.message : '索引を読み込めません') })
    }
    setNoteIndex({ notes: [], paths: [], backlinks: [], results: [], truncated: false })
    const timer = window.setTimeout(refresh, query ? 250 : 0)
    const interval = window.setInterval(refresh, 15000)
    return () => { active = false; window.clearTimeout(timer); window.clearInterval(interval) }
  }, [project, known, path, version, query])

  useEffect(() => {
    if (!fragment || !loaded || loaded.path !== path) return
    const line = loaded.text.split('\n').findIndex((value) => fragment.startsWith('^') ? value.trimEnd().endsWith(fragment)
      : value.replace(/^#{1,6}\s+/, '').replace(/\s+#+\s*$/, '').trim() === fragment)
    if (line >= 0) { setJump({ line: line + 1, tick: Date.now() }); setView('edit') }
    setFragment('')
  }, [fragment, loaded, path])

  // A remembered project may have been removed or renamed since the last visit
  useEffect(() => {
    if (projects && project !== null && !names.includes(project)) { setProject(null); setPath(null) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projects])

  useEffect(() => {
    try { localStorage.setItem(SAVED, JSON.stringify({ project, path, showHidden })) } catch { /* storage disabled or full */ }
  }, [project, path, showHidden])

  useEffect(() => {
    if (!dirty) return
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault() }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])

  const load = useCallback(async (target: string, file: string) => {
    window.clearTimeout(previewTimer.current)
    const current = ++serial.current
    setLoaded(null); setOther(null)
    setLoading(true)
    setProblem(null)
    setConflict(false)
    try {
      const data = await api.fileFull(target, file)
      if (current !== serial.current) return
      if (data.kind === 'text') {
        text.current = data.text
        setPreview(data.text)
        setOther(null)
        setLoaded({ path: file, text: data.text, hash: data.hash ?? null, truncated: data.truncated, serial: current })
      } else {
        setLoaded(null)
        setOther({ path: file, kind: data.kind, tooLarge: data.kind !== 'binary' && data.too_large })
      }
      setDirty(false)
    } catch (error) {
      if (current !== serial.current) return
      if (error instanceof ApiError && error.status === 401) { onError(error); return }
      setLoaded(null)
      setOther(null)
      setDirty(false)
      setProblem(error instanceof Error ? error.message : '読み込めませんでした')
    } finally {
      if (current === serial.current) setLoading(false)
    }
  }, [onError])

  useEffect(() => {
    if (known && path) void load(project!, path)
    else { serial.current++; setLoaded(null); setOther(null); setProblem(null); setDirty(false); setLoading(false) }
    // Reloading on `version` would discard edits: the refresh button reloads the tree, and asks before the file.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [known, project, path, load])

  /** Builds what is saved on disk: unsaved edits are not part of it. */
  const compile = useCallback(async (target: string, file: string) => {
    const current = ++buildSerial.current
    setBuilding(true)
    setBuildError(null)
    try {
      const result = await api.compile(target, file)
      if (current !== buildSerial.current) return
      setBuilt({ key: `${target}:${file}`, result })
      if (result.ok) setPdfVersion((value) => value + 1)
    } catch (error) {
      if (current !== buildSerial.current) return
      if (error instanceof ApiError && error.status === 401) onError(error)
      else setBuildError(error instanceof Error ? error.message : 'コンパイルできませんでした')
    } finally {
      if (current === buildSerial.current) setBuilding(false)
    }
  }, [onError])

  // Opening a Typst or LaTeX document builds it once, so the preview is never older than the file
  const loadedKey = loaded ? `${loaded.path}:${loaded.serial}` : null
  useEffect(() => {
    buildSerial.current++
    setBuilt(null); setBuildError(null); setBuilding(false); setLogOpen(false); setJump(null)
    if (canCompile && project && loaded && isCompiled(loaded.path)) void compile(project, loaded.path)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadedKey, canCompile, compile])

  const leave = () => !dirty || window.confirm('保存していない変更があります。破棄して移動しますか？')

  function open(next: string, heading = '') {
    setFolder(null)
    if (next === path) { setFragment(heading); return }
    if (!leave()) return
    setFragment(heading)
    setDirty(false)
    setPath(next)
    setForm(null)
    if (!window.matchMedia('(min-width: 1024px)').matches) setTreeOpen(false)
  }

  function changed(value: string) {
    text.current = value
    setDirty(true)
    window.clearTimeout(previewTimer.current)
    previewTimer.current = window.setTimeout(() => setPreview(value), 250)
  }
  useEffect(() => () => window.clearTimeout(previewTimer.current), [])

  async function save(overwrite = false) {
    if (!project || !loaded || !editable || saving) return
    const savingSerial = serial.current
    setSaving(true)
    setProblem(null)
    try {
      let base = loaded.hash
      if (overwrite) {
        const latest = await api.fileFull(project, loaded.path)
        if (latest.kind !== 'text' || !latest.hash) throw new Error('現在のファイルはテキストとして保存できません。読み込み直してください')
        base = latest.hash
      }
      const sent = text.current
      const result = await api.saveFile(project, loaded.path, sent, base)
      if (savingSerial !== serial.current) return
      // Keep typing while the request is in flight: only what was sent is saved
      setLoaded((value) => value && value.path === result.path ? { ...value, hash: result.hash } : value)
      setDirty(text.current !== sent)
      setConflict(false)
      setVersion((value) => value + 1)
      if (canCompile && isCompiled(result.path)) void compile(project, result.path)
    } catch (error) {
      if (savingSerial !== serial.current) return
      if (error instanceof ApiError && error.status === 409) setConflict(true)
      else if (error instanceof ApiError && error.status === 401) onError(error)
      else setProblem(error instanceof Error ? error.message : '保存できませんでした')
    } finally {
      setSaving(false)
    }
  }

  async function submitForm() {
    if (!project || !form) return
    const value = form.value.trim().replace(/^\/+|\/+$/g, '')
    if (!value) return
    try {
      if (form.kind === 'folder') await api.makeFolder(project, value)
      else if (form.kind === 'file') {
        if (!leave()) return
        await api.saveFile(project, value, '', null, true)
        setDirty(false)
        setPath(value)
      } else if (folder) {
        if (dirty && path && inside(path, folder)) { setProblem('改名の前に保存してください'); return }
        await api.moveFile(project, folder, value)
        // The open file moves with its folder
        if (path && inside(path, folder)) setPath(value + path.slice(folder.length))
        setFolder(value)
      } else if (path) {
        if (dirty) { setProblem('改名の前に保存してください'); return }
        await api.moveFile(project, path, value)
        setPath(value)
      }
      setForm(null)
      setVersion((current) => current + 1)
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onError(error)
      else setProblem(error instanceof Error ? error.message : '操作に失敗しました')
    }
  }

  async function remove() {
    if (!project) return
    try {
      if (folder) {
        const summary = await api.folderSummary(project, folder)
        const entries = summary.files + summary.folders
        const opened = !!path && inside(path, folder)
        const detail = entries ? `中のファイル${summary.files}件・フォルダ${summary.folders}件（${sizeText(summary.size)}）もすべて削除します。` : ''
        const unsaved = opened && dirty ? '開いているファイルの保存していない変更も失われます。' : ''
        if (!window.confirm(`フォルダ「${folder}」を削除しますか？${detail}${unsaved}この操作は元に戻せません。`)) return
        await api.deleteFile(project, folder, entries ? entries : undefined)
        if (opened) { setDirty(false); setPath(null) }
        setFolder(null)
      } else {
        if (!path || !window.confirm(`${path} を削除しますか？この操作は元に戻せません。`)) return
        await api.deleteFile(project, path)
        setDirty(false)
        setPath(null)
      }
      setVersion((current) => current + 1)
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onError(error)
      else setProblem(error instanceof Error ? error.message : '削除できませんでした')
    }
  }

  const compiled = !!loaded && canCompile && isCompiled(loaded.path)
  // A result belongs to the document it was built from: never show one file's PDF or errors beside another
  const build = built && loaded && built.key === `${project}:${loaded.path}` ? built.result : null
  const svg = !!loaded && isSvg(loaded.path)
  // A blob URL of the SVG being edited; an <img> never runs scripts inside it
  const svgUrl = useMemo(() => svg && !loaded?.truncated ? URL.createObjectURL(new Blob([preview], { type: 'image/svg+xml' })) : null,
    [svg, loaded?.truncated, preview])
  useEffect(() => () => { if (svgUrl) URL.revokeObjectURL(svgUrl) }, [svgUrl])
  const hasPreview = !!loaded && (isMarkdown(loaded.path) || compiled || svg)
  const shown: View = hasPreview ? view : 'edit'
  const marp = !!loaded && isMarkdown(loaded.path) && isMarp(preview)
  // What can be presented: a PDF file, the PDF built from the open document, or saved Marp slides
  const deck: DeckSpec | null = !project ? null
    : other?.kind === 'pdf' && !other.tooLarge ? { project, path: other.path, kind: 'pdf', version }
    : compiled && build?.has_pdf ? { project, path: loaded!.path, kind: 'pdf', version: pdfVersion }
    : marp ? { project, path: loaded!.path, kind: 'marp', version }
    : null
  const icon = 'rounded-lg p-2 text-muted hover:text-fg disabled:opacity-40'

  return <main className="flex min-w-0 flex-1 flex-col">
    <header className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2">
      <button onClick={onOpenMenu} className={`rounded-lg p-1.5 text-muted hover:text-fg ${sidebarHidden ? '' : 'md:hidden'}`}
        aria-label="会話一覧" title={sidebarHidden ? 'サイドバーを表示' : undefined}><Menu className="size-5" aria-hidden /></button>
      <button onClick={() => setTreeOpen((value) => !value)} aria-pressed={treeOpen} aria-label="ファイル一覧" title="ファイル一覧"
        className={`rounded-lg p-1.5 ${treeOpen ? 'bg-raised text-fg' : 'text-muted hover:text-fg'}`}><FolderTree className="size-5" aria-hidden /></button>
      <h1 className="hidden text-sm font-medium sm:block">Docs</h1>
      <select value={known ? project! : ''} aria-label="プロジェクト"
        onChange={(event) => { if (leave()) { setDirty(false); setProject(event.target.value || null); setPath(null); setFolder(null); setForm(null); setTreeOpen(true) } }}
        className="min-w-0 max-w-[8rem] rounded-lg border border-border bg-bg px-2 py-1 text-sm sm:max-w-xs">
        <option value="">プロジェクトを選択</option>
        {names.map((name) => <option key={name} value={name}>{name}</option>)}
      </select>
      <span className="order-last min-w-0 basis-full truncate text-xs text-muted sm:order-none sm:flex-1 sm:basis-0" title={path ?? undefined}>
        {path}{dirty && <span className="ml-1 text-warn" aria-label="未保存">●</span>}
      </span>
      <span className="flex-1 sm:hidden" />
      {deck?.kind === 'pdf' && <a href={api.pdfUrl(deck.project, deck.path, deck.version)} download={deck.path.split('/').pop()!.replace(/\.(typ|tex)$/i, '.pdf')}
        className="rounded-lg border border-border px-2 py-1.5 text-sm text-muted">PDF保存</a>}
      {deck && <button aria-label="発表" onClick={() => setPresenting(deck)} title={dirty ? '保存済みの内容で発表します' : '発表者ビューを開く'}
        className="flex items-center gap-1 rounded-lg border border-border px-2 py-1.5 text-sm text-muted hover:text-fg">
        <Presentation className="size-4" aria-hidden /><span className="hidden sm:inline">発表</span>
      </button>}
      {hasPreview && <div className="flex rounded-lg border border-border" role="group" aria-label="表示の切り替え">
        {([['edit', '編集', SquarePen], ['split', '分割', Columns2], ['preview', '表示', Eye]] as const).map(([value, label, Icon]) =>
          <button key={value} onClick={() => { setView(value); if (!window.matchMedia('(min-width: 1024px)').matches) setTreeOpen(false) }} aria-pressed={view === value} aria-label={label} title={label}
            className={`p-1.5 ${value === 'split' ? 'hidden lg:block' : ''} ${view === value ? 'bg-raised text-fg' : 'text-muted hover:text-fg'}`}>
            <Icon className="size-4" aria-hidden />
          </button>)}
      </div>}
      {loaded && <button onClick={() => void save()} disabled={!editable || !dirty || saving}
        title={canEdit ? '保存（Ctrl+S / ⌘S）' : '編集は無効です'}
        className="flex items-center gap-1 rounded-lg bg-accent px-3 py-1.5 text-sm text-accent-fg disabled:opacity-40">
        <Save className="size-4" aria-hidden />{saving ? '保存中…' : '保存'}
      </button>}
    </header>

    {!canEdit && <p className="border-b border-border bg-surface px-3 py-1.5 text-xs text-muted">
      閲覧のみです。編集するには .env の OMNI_ALLOW_FILE_EDIT=true と、設定画面の「ファイルの編集」が必要です。
    </p>}
    {problem && <p role="alert" className="flex items-start gap-3 border-b border-border px-3 py-2 text-sm text-danger">
      <span className="flex-1">{problem}</span><button onClick={() => setProblem(null)} className="text-muted">閉じる</button>
    </p>}
    {conflict && <div role="alert" className="flex flex-wrap items-center gap-2 border-b border-border bg-surface px-3 py-2 text-sm">
      <span className="flex-1">このファイルは読み込み後に変更されています（エージェントや別の画面による変更）。</span>
      <button onClick={() => { if (project && path && window.confirm('編集中の内容を破棄して読み込み直しますか？')) void load(project, path) }}
        className="rounded-lg border border-border px-2 py-1">読み込み直す</button>
      <button onClick={() => { if (window.confirm('ファイル側の変更を上書きして保存しますか？')) void save(true) }}
        className="rounded-lg border border-danger px-2 py-1 text-danger">上書き保存</button>
    </div>}

    <div className="flex min-h-0 flex-1">
      {treeOpen && <aside aria-label="ファイル一覧" style={widthVar('tree', tree.width)}
        className="flex w-full shrink-0 flex-col border-r border-border lg:w-[var(--tree-w)]">
        {known ? <>
          <div className="flex items-center border-b border-border px-1">
            {canEdit && <>
              <button onClick={() => setForm({ kind: 'file', value: folder ? `${folder}/` : folderOf(path ?? '') })} className={icon} aria-label="新しいファイル" title="新しいファイル"><FilePlus className="size-4" aria-hidden /></button>
              <button onClick={() => setForm({ kind: 'folder', value: folder ? `${folder}/` : folderOf(path ?? '') })} className={icon} aria-label="新しいフォルダ" title="新しいフォルダ"><FolderPlus className="size-4" aria-hidden /></button>
              <button onClick={() => { const target = folder ?? path; if (target) setForm({ kind: 'rename', value: target }) }} disabled={!folder && !path} className={icon}
                aria-label={folder ? 'フォルダを改名・移動' : '改名・移動'} title={folder ? `フォルダ「${folder}」を改名・移動` : '改名・移動'}><Pencil className="size-4" aria-hidden /></button>
              <button onClick={() => void remove()} disabled={!folder && !path} className={icon}
                aria-label={folder ? 'フォルダを削除' : '削除'} title={folder ? `フォルダ「${folder}」を削除` : '削除'}><Trash2 className="size-4" aria-hidden /></button>
            </>}
            <span className="flex-1" />
            <button onClick={() => setVersion((value) => value + 1)} className={icon} aria-label="ファイル一覧を更新" title="ファイル一覧を更新"><RefreshCw className="size-4" aria-hidden /></button>
          </div>
          <div className="space-y-2 border-b border-border p-2">
            <label className="flex items-center gap-2 text-xs text-muted" title=". で始まるファイル・フォルダを一覧に表示します。秘密情報などのアクセス制限は変わりません。">
              <input type="checkbox" checked={showHidden} onChange={(event) => setShowHidden(event.target.checked)} />
              ドットファイルを表示
            </label>
            <input type="search" aria-label="ノートを検索" placeholder="ノートの名前・本文・タグを検索" value={query}
              onChange={(event) => setQuery(event.target.value)} className="w-full rounded border border-border bg-bg px-2 py-1 text-xs" />
            {indexError && <p role="alert" className="text-xs text-danger">{indexError}</p>}
            {noteIndex.truncated && <p className="text-xs text-muted">索引の上限に達しました。一部のノートは対象外です。</p>}
            {query && <ul aria-label="検索結果" className="max-h-60 overflow-auto text-xs">
              {searchResults.map((item) => <li key={item.path}><button className="w-full rounded p-1 text-left hover:bg-raised" onClick={() => open(item.path)}>
                <span className="block text-accent">{item.path}</span><span className="block truncate text-muted">{item.snippet}</span>
              </button></li>)}
              {!searchResults.length && <li className="text-muted">一致するノートがありません</li>}
            </ul>}
            {path && <details open><summary className="cursor-pointer text-xs text-muted">バックリンク（{backlinks.length}）</summary>
              <ul aria-label="バックリンク" className="max-h-32 overflow-auto text-xs">{backlinks.map((item) => <li key={item.path}>
                <button className="truncate text-accent" onClick={() => open(item.path)}>{item.path}</button>
              </li>)}</ul>
            </details>}
          </div>
          {form && <form onSubmit={(event) => { event.preventDefault(); void submitForm() }} className="flex flex-col gap-1.5 border-b border-border p-2">
            <label className="text-xs text-muted" htmlFor="docs-path">
              {form.kind === 'file' ? '新しいファイルのパス' : form.kind === 'folder' ? '新しいフォルダのパス' : '新しいパス'}（プロジェクトからの相対パス）
            </label>
            <input id="docs-path" autoFocus value={form.value} onChange={(event) => setForm({ ...form, value: event.target.value })}
              placeholder={form.kind === 'folder' ? 'docs/notes' : 'docs/notes.md'}
              className="rounded-lg border border-border bg-bg px-2 py-1 text-sm" />
            <div className="flex justify-end gap-2 text-sm">
              <button type="button" onClick={() => setForm(null)} className="px-2 py-1 text-muted">やめる</button>
              <button type="submit" className="rounded-lg bg-accent px-3 py-1 text-accent-fg">{form.kind === 'rename' ? '変更' : '作成'}</button>
            </div>
          </form>}
          <div className="min-h-0 flex-1 overflow-y-auto py-1">
            <Tree project={project!} path="" depth={0} version={version} selected={folder ?? path} onSelect={open} onFolder={setFolder} showHidden={showHidden} />
          </div>
        </> : <p className="p-4 text-sm text-muted">{projects ? 'プロジェクトを選択してください' : '読み込み中…'}</p>}
      </aside>}

      {treeOpen && <Resizer label="ファイル一覧の幅" edge="right" width={tree.width} limits={TREE} others={sideWidth}
        onChange={tree.setWidth} onReset={tree.reset} className="hidden lg:block" />}
      <div className={`min-h-0 min-w-0 flex-1 ${treeOpen ? 'hidden lg:flex' : 'flex'}`}>
        {!path && <p className="p-4 text-sm text-muted">ファイルを選択すると、ここで編集できます</p>}
        {path && loading && !loaded && <p className="p-4 text-sm text-muted">読み込み中…</p>}
        {other && (other.kind === 'image' && !other.tooLarge
          ? <Suspense fallback={<p className="p-4 text-sm text-muted">読み込み中…</p>}><ImageView src={api.imageUrl(project!, other.path, version)} name={other.path} /></Suspense>
          : other.kind === 'pdf' && !other.tooLarge
          ? <div className="flex-1 overflow-auto bg-surface"><Suspense fallback={<p className="p-4 text-sm text-muted">読み込み中…</p>}><PdfView url={api.pdfUrl(project!, other.path, version)} /></Suspense></div>
          : <p className="p-4 text-sm text-muted">{other.kind === 'image' ? '画像は32 MiBまで表示できます' : other.kind === 'pdf' ? 'PDFは64 MiBまで表示できます' : 'この形式は表示・編集できません'}</p>)}
        {loaded && <>
          <div className={`min-h-0 min-w-0 flex-1 flex-col ${shown === 'preview' ? 'hidden' : 'flex'}`}>
            {loaded.truncated && <p className="border-b border-border px-3 py-1.5 text-xs text-muted">2 MiBを超えるため先頭だけを表示しています。このファイルは編集できません。</p>}
            <Suspense fallback={<p className="p-4 text-sm text-muted">エディタを読み込み中…</p>}>
              <Editor docKey={`${project}:${loaded.path}:${loaded.serial}`} path={loaded.path} text={loaded.text}
                readOnly={!editable} jump={jump} notePaths={noteIndex.notes.filter(listed).map((note) => note.path)} onChange={changed} onSave={() => void save()} />
            </Suspense>
          </div>
          {hasPreview && shown === 'split' && <Resizer label="プレビューの幅" edge="left" width={previewSize.width} limits={PREVIEW}
            others={sideWidth + (treeOpen ? tree.width : 0)} onChange={previewSize.setWidth} onReset={previewSize.reset} className="hidden lg:block" />}
          {hasPreview && <div aria-label="プレビュー" tabIndex={0} style={widthVar('preview', previewSize.width)}
            className={`min-h-0 min-w-0 flex-1 flex-col overflow-y-auto border-border lg:border-l ${compiled ? 'bg-surface' : svg ? '' : 'p-4'} ${shown === 'edit' ? 'hidden' : shown === 'split' ? 'hidden lg:flex lg:w-[var(--preview-w)] lg:flex-none' : 'flex'}`}>
            {svg ? <Suspense fallback={<p className="p-4 text-sm text-muted">読み込み中…</p>}>
              {/* The editor's text, so edits show before saving; a truncated file is shown as saved */}
              <ImageView src={loaded.truncated ? api.imageUrl(project!, loaded.path, version) : svgUrl ?? ''} name={loaded.path} vector openable={loaded.truncated} />
            </Suspense> : compiled ? <>
              <div className="sticky top-0 z-10 border-b border-border bg-bg text-sm">
                <div className="flex items-center gap-2 px-3 py-1.5">
                  <span role="status" className={`min-w-0 flex-1 truncate ${build && !build.ok ? 'text-danger' : 'text-muted'}`}>
                    {building ? 'コンパイル中…' : buildError ? buildError : !build ? '' : build.ok ? `コンパイル成功（${build.seconds}秒）`
                      : `コンパイル失敗${build.has_pdf ? '（前回成功したPDFを表示中）' : ''}`}
                    {dirty && !building && '　未保存の変更は含まれません'}
                  </span>
                  {build && <button onClick={() => setLogOpen((value) => !value)} aria-expanded={logOpen} className="shrink-0 rounded-lg border border-border px-2 py-0.5 text-xs">ログ</button>}
                  <button onClick={() => void compile(project!, loaded.path)} disabled={building} className="flex shrink-0 items-center gap-1 rounded-lg border border-border px-2 py-0.5 text-xs disabled:opacity-40">
                    <Play className="size-3" aria-hidden />再コンパイル
                  </button>
                </div>
                {build && build.diagnostics.length > 0 && <ul className="max-h-40 overflow-y-auto border-t border-border">
                  {build.diagnostics.map((item, index) => {
                    const here = item.path === loaded.path && item.line !== null
                    return <li key={index}>
                      <button disabled={!here} onClick={() => { setJump({ line: item.line!, tick: Date.now() }); if (view === 'preview') setView('edit') }}
                        className={`flex w-full gap-2 px-3 py-1 text-left text-xs ${here ? 'hover:bg-raised' : ''}`}>
                        <span className={`shrink-0 ${item.severity === 'error' ? 'text-danger' : 'text-warn'}`}>{item.severity === 'error' ? 'エラー' : '警告'}</span>
                        <span className="shrink-0 font-mono text-muted">{item.path && item.path !== loaded.path ? `${item.path}:` : ''}{item.line ?? ''}</span>
                        <span className="min-w-0 break-words">{item.message}</span>
                      </button>
                    </li>
                  })}
                </ul>}
                {logOpen && build && <pre className="max-h-60 overflow-auto border-t border-border p-2 font-mono text-[11px] leading-4">{build.log || '（出力なし）'}</pre>}
              </div>
              {build?.has_pdf && <Suspense fallback={<p className="p-4 text-sm text-muted">読み込み中…</p>}>
                <PdfView url={api.pdfUrl(project!, loaded.path, pdfVersion)} />
              </Suspense>}
            </> : marp ? <Suspense fallback={<p className="p-4 text-sm text-muted">読み込み中…</p>}><MarpPreview text={preview} project={project!} path={loaded.path} /></Suspense>
            : <div className="mx-auto w-full max-w-3xl">
              <NoteMarkdown text={preview} project={project!} path={loaded.path} index={noteIndex} version={version}
                onOpen={open} onSearch={(value) => { setQuery(value); setTreeOpen(true) }} />
            </div>}
          </div>}
        </>}
      </div>
    </div>
    {presenting && <Suspense fallback={null}><Presenter spec={presenting} onClose={() => setPresenting(null)} /></Suspense>}
  </main>
}
