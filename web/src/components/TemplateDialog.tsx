import { useCallback, useEffect, useState } from 'react'
import { ArrowLeft, Copy, FilePlus2, FolderInput, Pencil, Plus, X } from 'lucide-react'
import { api, type TemplateInfo, type TemplatePlan } from '../api'
import { useBackClose } from '../backClose'

const STATUS: Record<string, string> = { new: '追加', changed: '内容が違う', same: '同じ内容', blocked: '追加できない' }
const failure = (error: unknown) => error instanceof Error ? error.message : '処理に失敗しました'

/**
 * Templates for the project open in Docs: add one to the project (after seeing what each file would do),
 * or make and edit the user's own. A template of the user's own is a project, so "edit" just opens it in Docs.
 */
export default function TemplateDialog({ project, canEdit, onClose, onApplied, onOpenProject }: {
  project: string | null
  canEdit: boolean
  onClose: () => void
  /** Files were written into the project */
  onApplied: () => void
  /** Show this template's own folder in Docs for editing */
  onOpenProject: (name: string) => void
}) {
  const [templates, setTemplates] = useState<TemplateInfo[] | null>(null)
  const [plan, setPlan] = useState<TemplatePlan | null>(null)
  const [chosen, setChosen] = useState<Set<string>>(new Set())
  const [expanded, setExpanded] = useState<string | null>(null)
  const [creating, setCreating] = useState<{ source: string | null; name: string; title: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)
  useBackClose(true, onClose)

  const reload = useCallback(() => {
    api.templates().then((data) => setTemplates(data.templates)).catch((reason) => setError(failure(reason)))
  }, [])
  useEffect(reload, [reload])

  async function run(action: () => Promise<void>) {
    setBusy(true)
    setError(null)
    try { await action() } catch (reason) { setError(failure(reason)) } finally { setBusy(false) }
  }

  const openPlan = (name: string) => run(async () => {
    const data = await api.templatePlan(name, project!)
    setPlan(data)
    setDone(null)
    setExpanded(null)
    // New files are added unless unticked; a file that differs is replaced only when the user ticks it
    setChosen(new Set(data.files.filter((file) => file.status === 'new').map((file) => file.path)))
  })

  const apply = () => run(async () => {
    const files = plan!.files.filter((file) => chosen.has(file.path)).map((file) => ({ path: file.path, base_hash: file.hash }))
    const result = await api.applyTemplate(plan!.template.name, project!, files)
    const failed = result.files.filter((file) => !file.ok)
    onApplied()
    if (failed.length) setError(failed.map((file) => `${file.path}: ${file.message}`).join('\n'))
    setDone(`${result.files.length - failed.length}個のファイルを反映しました`)
    setPlan(await api.templatePlan(plan!.template.name, project!))
    setChosen(new Set())
  })

  const create = () => run(async () => {
    const made = await api.createTemplate(creating!.name.trim(), creating!.source)
    setCreating(null)
    if (made.project) onOpenProject(made.project)
  })

  const button = 'flex items-center gap-1 rounded-lg border border-border px-2 py-1 text-xs hover:bg-raised disabled:opacity-40'
  return <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-0 sm:p-4" onClick={onClose}>
    <section role="dialog" aria-modal aria-label="テンプレート" onClick={(event) => event.stopPropagation()}
      onKeyDown={(event) => { if (event.key === 'Escape') onClose() }}
      className="flex h-full w-full max-w-2xl flex-col bg-bg sm:h-auto sm:max-h-[85vh] sm:rounded-2xl sm:border sm:border-border">
      <header className="flex items-center gap-2 border-b border-border px-4 py-3">
        {(plan || creating) && <button onClick={() => { setPlan(null); setCreating(null); setError(null) }} className="rounded p-1 text-muted" aria-label="一覧へ戻る"><ArrowLeft className="size-4" aria-hidden /></button>}
        <h2 className="min-w-0 flex-1 truncate font-semibold">
          {plan ? `「${plan.template.label}」を ${plan.project} に追加` : creating ? creating.title : 'テンプレート'}
        </h2>
        <button onClick={onClose} className="rounded p-1 text-muted" aria-label="閉じる"><X className="size-4" aria-hidden /></button>
      </header>
      {error && <p role="alert" className="whitespace-pre-wrap border-b border-border px-4 py-2 text-sm text-danger">{error}</p>}
      {done && <p role="status" className="border-b border-border px-4 py-2 text-sm text-ok">{done}</p>}

      {creating ? <form onSubmit={(event) => { event.preventDefault(); void create() }} className="space-y-3 p-4 text-sm">
        <label className="block">テンプレートの名前（フォルダ名になります）
          <input autoFocus value={creating.name} onChange={(event) => setCreating({ ...creating, name: event.target.value })}
            placeholder="例: weekly-report" className="mt-1 w-full rounded-lg border border-border bg-bg px-3 py-2" />
        </label>
        <p className="text-xs text-muted">作成するとDocsで開きます。フォルダに置いたファイルがそのままテンプレートの中身になり、TEMPLATE.md の見出しと最初の段落が一覧の名前と説明になります。</p>
        <div className="flex justify-end">
          <button type="submit" disabled={busy || !creating.name.trim()} className="rounded-lg bg-accent px-4 py-2 text-accent-fg disabled:opacity-50">作成して編集</button>
        </div>
      </form>

      : plan ? <>
        <ul className="min-h-0 flex-1 overflow-y-auto">
          {plan.files.length === 0 && <li className="p-4 text-sm text-muted">このテンプレートにはファイルがありません</li>}
          {plan.files.map((file) => {
            const selectable = file.status === 'new' || file.status === 'changed'
            return <li key={file.path} className="border-b border-border px-4 py-2 text-sm">
              <div className="flex items-center gap-2">
                <input type="checkbox" id={`template-${file.path}`} disabled={!selectable} checked={chosen.has(file.path)}
                  onChange={(event) => setChosen((old) => { const next = new Set(old); if (event.target.checked) next.add(file.path); else next.delete(file.path); return next })} />
                <label htmlFor={`template-${file.path}`} className="min-w-0 flex-1 break-all font-mono text-xs">{file.path}</label>
                <span className={`shrink-0 text-xs ${file.status === 'new' ? 'text-ok' : file.status === 'changed' ? 'text-warn' : 'text-muted'}`}>
                  {file.status === 'changed' && chosen.has(file.path) ? '上書きする' : STATUS[file.status]}
                </span>
                {file.diff && <button onClick={() => setExpanded(expanded === file.path ? null : file.path)} aria-expanded={expanded === file.path} className={button}>差分</button>}
              </div>
              {expanded === file.path && file.diff && <pre className="mt-2 max-h-64 overflow-auto rounded-lg border border-border font-mono text-xs leading-5"><code>
                {file.diff.split('\n').map((line, index) => <span key={index} className={`block px-2 ${line.startsWith('@@') ? 'text-accent'
                  : line.startsWith('+++') || line.startsWith('---') ? 'text-muted'
                  : line.startsWith('+') ? 'bg-ok/10 text-ok' : line.startsWith('-') ? 'bg-danger/10 text-danger' : ''}`}>{line || ' '}</span>)}
              </code></pre>}
            </li>
          })}
        </ul>
        <footer className="flex items-center gap-3 border-t border-border px-4 py-3 text-sm">
          <span className="min-w-0 flex-1 text-xs text-muted">差分の「-」は今のファイル、「+」はテンプレートの内容です。内容が違うファイルは、選んだものだけ上書きします。</span>
          <button onClick={() => void apply()} disabled={busy || chosen.size === 0} className="shrink-0 rounded-lg bg-accent px-4 py-2 text-accent-fg disabled:opacity-50">
            {chosen.size}個を反映
          </button>
        </footer>
      </>

      : <>
        <ul className="min-h-0 flex-1 overflow-y-auto">
          {!templates && !error && <li className="p-4 text-sm text-muted">読み込み中…</li>}
          {templates?.map((template) => <li key={template.name} className="border-b border-border px-4 py-3">
            <div className="flex items-baseline gap-2">
              <h3 className="min-w-0 truncate text-sm font-medium">{template.label}</h3>
              <span className="shrink-0 rounded bg-raised px-1.5 text-[11px] text-muted">{template.source === 'user' ? '自分用' : 'サンプル'}</span>
              <span className="shrink-0 text-xs text-muted">{template.files.length}ファイル</span>
            </div>
            {template.description && <p className="mt-1 text-sm text-muted">{template.description}</p>}
            <p className="mt-1 truncate font-mono text-[11px] text-muted" title={template.files.join('\n')}>{template.files.join('　')}</p>
            <div className="mt-2 flex flex-wrap gap-2">
              <button onClick={() => void openPlan(template.name)} disabled={busy || !project || !canEdit || project === template.project}
                title={!project ? 'Docsでプロジェクトを選ぶと追加できます' : undefined} className={button}>
                <FolderInput className="size-3.5" aria-hidden />{project && project !== template.project ? `${project.split('/').pop()} に追加` : 'プロジェクトに追加'}
              </button>
              {template.source === 'user' && template.project
                ? <button onClick={() => onOpenProject(template.project!)} className={button}><Pencil className="size-3.5" aria-hidden />編集</button>
                : <button disabled={!canEdit} onClick={() => setCreating({ source: `template:${template.name}`, name: template.name, title: `「${template.label}」を複製して編集` })} className={button}>
                    <Copy className="size-3.5" aria-hidden />複製して編集
                  </button>}
            </div>
          </li>)}
        </ul>
        <footer className="flex flex-wrap items-center gap-2 border-t border-border px-4 py-3">
          <button disabled={!canEdit} onClick={() => setCreating({ source: null, name: '', title: '新しいテンプレート' })} className={button}><Plus className="size-3.5" aria-hidden />新しいテンプレート</button>
          <button disabled={!canEdit || !project} onClick={() => setCreating({ source: `project:${project}`, name: '', title: `${project} をテンプレートにする` })} className={button}>
            <FilePlus2 className="size-3.5" aria-hidden />このプロジェクトをテンプレートにする
          </button>
          {!canEdit && <span className="text-xs text-muted">作成と追加には「ファイルの編集」の許可が必要です</span>}
        </footer>
      </>}
    </section>
  </div>
}
