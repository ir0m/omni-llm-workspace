import { useEffect, useState } from 'react'
import { api, type Executor, type ProjectBranches, type Worktree, type WorktreeResult } from '../api'

type Task = { title: string; text: string; provider: string; model: string }
const field = 'w-full rounded border border-border bg-bg px-2 py-2 text-sm'

export default function WorktreePanel({ project, onChanged }: { project: string; onChanged: () => void }) {
  const [items, setItems] = useState<Worktree[]>([])
  const [executors, setExecutors] = useState<Executor[]>([])
  const [allowed, setAllowed] = useState(false)
  const [same, setSame] = useState(false)
  const [shared, setShared] = useState('')
  const [base, setBase] = useState('')
  const [branches, setBranches] = useState<ProjectBranches | null>(null)
  const [tasks, setTasks] = useState<Task[]>([{ title: '', text: '', provider: '', model: '' }])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<string[]>([])
  const [results, setResults] = useState<Record<string, WorktreeResult>>({})
  const [patch, setPatch] = useState('')
  function loadBranches(data: ProjectBranches) {
    setBranches(data)
    setBase((previous) => data.branches.some((branch) => branch.ref === previous) || previous === data.head
      ? previous : data.current_ref ?? data.head ?? '')
  }
  async function refresh() {
    const [data, refs] = await Promise.all([api.worktrees(project), api.branches(project)])
    setItems(data.items); setAllowed(data.can_create); loadBranches(refs); onChanged()
  }
  useEffect(() => {
    let live = true
    Promise.all([api.worktrees(project), api.executors(), api.branches(project)]).then(([data, choices, refs]) => {
      if (live) { setItems(data.items); setAllowed(data.can_create); loadBranches(refs); setExecutors(choices.filter((e) => e.configured && e.modes.includes('code'))) }
    }).catch((e) => { if (live) setError(e.message) })
    return () => { live = false }
  }, [project])
  function update(index: number, changes: Partial<Task>) { setTasks((old) => old.map((task, i) => i === index ? { ...task, ...changes } : task)) }
  async function create(start: boolean) {
    setBusy(true); setError('')
    const created: { item: Worktree; task: Task }[] = []
    try {
      let pinnedBase = base
      for (const [index, task] of tasks.entries()) {
        const item = await api.createWorktree(project, task.title.trim() || `作業 ${index + 1}`, pinnedBase)
        pinnedBase = item.base
        created.push({ item, task })
      }
      if (start) {
        const replies = await Promise.allSettled(created.map(({ item, task }) => api.send(item.conversation_id, {
          text: same ? shared : task.text, provider: task.provider, model: task.model,
        })))
        const failed = replies.flatMap((reply, i) => reply.status === 'rejected' ? [`${created[i].item.title}: ${reply.reason.message}`] : [])
        if (failed.length) setError(failed.join('\n'))
      }
      setTasks([{ title: '', text: '', provider: '', model: '' }]); setShared('')
    } catch (e) { setError(`${(e as Error).message}\n作成済みの作業は下の一覧から再開できます。`) }
    finally { try { await refresh() } catch (e) { setError((e as Error).message) }; setBusy(false) }
  }
  async function compare() {
    setBusy(true); setError(''); setPatch('')
    try {
      const rows = await Promise.all(selected.map(async (name) => [name, await api.worktreeResult(name)] as const))
      setResults(Object.fromEntries(rows)); await refresh()
    } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }
  async function action(item: Worktree, remove: boolean) {
    if (!window.confirm(remove ? '作業フォルダを削除します。会話とブランチは残ります。続けますか？' : 'コミット済みの変更を元フォルダの現在のブランチに統合します。続けますか？')) return
    setBusy(true); setError('')
    try { if (remove) await api.removeWorktree(item.name); else await api.mergeWorktree(item.name); await refresh() }
    catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }
  const valid = tasks.every((task) => task.provider && task.model && (same ? shared : task.text).trim())
  return <div className="min-h-0 flex-1 overflow-auto p-3 text-sm">
    <p className="mb-3 text-xs text-muted">作業ごとにフォルダ・ブランチ・会話を分けます。開始地点のコミットを使い、未コミット変更・未追跡ファイル（.env など）はコピーしません。</p>
    {error && <p role="alert" className="mb-3 whitespace-pre-wrap text-danger">{error}</p>}
    <fieldset disabled={busy || !allowed} className="space-y-3 disabled:opacity-60">
      <label className="block">開始地点のブランチ
        <select aria-label="開始地点のブランチ" className={field} value={base} onChange={(e) => setBase(e.target.value)}>
          {!base && <option value="">{branches ? '選択できるブランチがありません' : '読み込み中…'}</option>}
          {!branches?.current_ref && branches?.head && <option value={branches.head}>現在のコミット（{branches.head.slice(0, 8)}）</option>}
          {[false, true].map((remote) => <optgroup key={String(remote)} label={remote ? 'リモート追跡ブランチ' : 'ローカルブランチ'}>
            {branches?.branches.filter((branch) => branch.remote === remote).map((branch) =>
              <option key={branch.ref} value={branch.ref}>{branch.name}{branch.ref === branches.current_ref ? '（現在）' : ''}</option>)}
          </optgroup>)}
        </select>
      </label>
      {branches && !base && <p className="text-xs text-muted">最初のコミットを作成してから一覧を更新してください。</p>}
      <label className="flex gap-2"><input type="checkbox" checked={same} onChange={(e) => setSame(e.target.checked)} />全員に同じ指示を送る</label>
      {same && <textarea aria-label="共通の指示" placeholder="比較したい作業の指示" className={field} value={shared} onChange={(e) => setShared(e.target.value)} />}
      {tasks.map((task, index) => <div key={index} className="space-y-2 rounded border border-border p-2">
        <label className="block">作業 {index + 1}<input aria-label={`作業 ${index + 1} の名前`} placeholder="作業名" className={field} value={task.title} onChange={(e) => update(index, { title: e.target.value })} /></label>
        <select aria-label={`作業 ${index + 1} の実行元`} className={field} value={task.provider} onChange={(e) => {
          const executor = executors.find((item) => item.id === e.target.value)
          update(index, { provider: e.target.value, model: executor?.default?.model || executor?.models[0]?.id || '' })
        }}><option value="">実行元を選択</option>{executors.map((e) => <option key={e.id} value={e.id}>{e.label}</option>)}</select>
        <select aria-label={`作業 ${index + 1} のモデル`} className={field} value={task.model} onChange={(e) => update(index, { model: e.target.value })}>
          <option value="">モデルを選択</option>{executors.find((e) => e.id === task.provider)?.models.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
        </select>
        {!same && <textarea aria-label={`作業 ${index + 1} の指示`} className={field} placeholder="この作業への指示" value={task.text} onChange={(e) => update(index, { text: e.target.value })} />}
        {tasks.length > 1 && <button onClick={() => setTasks((old) => old.filter((_, i) => i !== index))}>この行を外す</button>}
      </div>)}
      <div className="flex flex-wrap gap-2">
        <button disabled={tasks.length >= 4} onClick={() => setTasks([...tasks, { title: '', text: '', provider: '', model: '' }])} className="rounded border border-border px-3 py-2">作業を追加</button>
        <button disabled={!base} onClick={() => create(false)} className="rounded border border-border px-3 py-2 disabled:opacity-40">フォルダと会話を作成</button>
        <button disabled={!valid || !base} onClick={() => create(true)} className="rounded bg-accent px-3 py-2 text-white disabled:opacity-40">作成して同時に開始</button>
      </div>
    </fieldset>
    {!allowed && <p className="mt-2 text-muted">フォルダ作成・コマンド実行の許可が必要です。</p>}
    <div className="my-3 flex gap-3"><button disabled={busy} onClick={() => refresh().catch((e) => setError(e.message))}>一覧を更新</button><button disabled={busy || !selected.length} onClick={compare}>選択した結果を比較・更新</button></div>
    <div className="grid gap-3">
      {items.map((item) => <article key={item.name} className="min-w-0 rounded border border-border p-3">
        <label className="flex gap-2"><input type="checkbox" aria-label={`${item.title}を比較`} checked={selected.includes(item.name)} onChange={(e) => setSelected((old) => e.target.checked ? [...old, item.name] : old.filter((name) => name !== item.name))} />{item.title}</label>
        <p className="my-1 break-all text-xs text-muted">{item.branch}・{item.conversation?.active_run ? '実行中' : '待機／終了'}</p>
        <div className="flex flex-wrap gap-3 text-xs">
          {item.conversation && <a className="text-accent" href={`#/c/${item.conversation_id}`}>会話・承認を開く</a>}
          <button disabled={busy} onClick={() => action(item, false)}>元フォルダへ統合</button>
          <button disabled={busy} onClick={() => action(item, true)}>フォルダを削除</button>
        </div>
        {results[item.name] && <div className="mt-3 space-y-2">
          <p className="max-h-52 overflow-auto whitespace-pre-wrap break-words">{results[item.name].answer || '返答はまだありません'}</p>
          <p className="text-xs text-muted">開始地点からの変更（コミット済みを含む）</p>
          {results[item.name].paths.map((path) => <button key={path} className="block break-all text-left text-accent" onClick={() => api.worktreeDiff(item.name, path).then((d) => setPatch(`${item.title}: ${path}\n${d.patch}${d.truncated ? '\n（先頭部分のみ）' : ''}`)).catch((e) => setError(e.message))}>{path}</button>)}
          {results[item.name].changes.untracked.map(({ path }) => <button key={path} className="block break-all text-left text-accent" onClick={() => api.diff(item.name, path, 'untracked').then((d) => setPatch(`${item.title}: ${path}\n${d.patch}`)).catch((e) => setError(e.message))}>{path}（未追跡）</button>)}
        </div>}
      </article>)}
    </div>
    {patch && <pre tabIndex={0} aria-label="作業結果の差分" className="mt-3 max-h-96 overflow-auto rounded bg-raised p-2 text-xs">{patch}</pre>}
    <p className="mt-3 text-xs text-muted">統合はコミット済みで両方のフォルダがクリーンな場合のみ。ブランチが分岐している場合は端末で統合してください。</p>
  </div>
}
