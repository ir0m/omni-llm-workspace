import { useCallback, useEffect, useRef, useState } from 'react'
import { ChevronLeft, ChevronRight, ExternalLink, RefreshCw, Server, Settings, Shield, UserRound, MessagesSquare, X } from 'lucide-react'
import {
  api, type Accounts, type ApprovalMode, type Executor, type OutsideLimit, type PermissionValues, type Permissions,
  type ServerStatus, type SessionCounts, type SubscriptionUsage,
} from '../api'
import { UsageRow } from './UsageMeter'

type Category = 'accounts' | 'permissions' | 'server' | 'sessions'
const CATEGORIES: { id: Category; label: string; icon: typeof Settings }[] = [
  { id: 'accounts', label: 'アカウント・CLI', icon: UserRound },
  { id: 'permissions', label: '権限', icon: Shield },
  { id: 'server', label: 'サーバー', icon: Server },
  { id: 'sessions', label: 'セッション', icon: MessagesSquare },
]

type Props = {
  executors: Executor[]
  onClose: () => void
  /** Permissions, logins or model defaults changed: reload what the rest of the screen shows */
  onChanged: () => void
  onDiscover: () => void
  onDeletedAll: () => void
}

const failure = (error: unknown) => (error instanceof Error ? error.message : '失敗しました')
const button = 'shrink-0 rounded-lg border border-border px-3 py-1.5 text-sm hover:bg-raised disabled:opacity-40'
const select = 'max-w-full rounded-lg border border-border bg-bg px-2 py-1.5 text-sm disabled:opacity-50'

export default function SettingsDialog(props: Props) {
  const [category, setCategory] = useState<Category | null>(() => (window.matchMedia('(min-width: 768px)').matches ? 'accounts' : null))
  const dialog = useRef<HTMLDivElement>(null)
  const close = useRef(props.onClose)
  close.current = props.onClose

  useEffect(() => {
    const key = (event: KeyboardEvent) => { if (event.key === 'Escape') close.current() }
    window.addEventListener('keydown', key)
    dialog.current?.focus()
    return () => window.removeEventListener('keydown', key)
  }, [])

  const current = CATEGORIES.find((item) => item.id === category)
  return (
    <div className="fixed inset-0 z-50 flex items-stretch justify-center bg-black/40 md:items-center md:p-6" onClick={props.onClose}>
      <div ref={dialog} tabIndex={-1} role="dialog" aria-modal="true" aria-label="設定" onClick={(event) => event.stopPropagation()}
        className="flex h-full w-full flex-col overflow-hidden bg-bg outline-none md:h-[85vh] md:max-w-4xl md:rounded-2xl md:border md:border-border">
        <header className="flex items-center gap-2 border-b border-border px-3 py-2">
          {category && <button onClick={() => setCategory(null)} className="rounded-lg p-1.5 text-muted md:hidden" aria-label="設定の一覧へ戻る">
            <ChevronLeft className="size-5" aria-hidden />
          </button>}
          <h2 className="flex flex-1 items-center gap-2 font-semibold">
            <Settings className="size-4" aria-hidden />
            <span className="md:hidden">{current ? current.label : '設定'}</span>
            <span className="hidden md:inline">設定</span>
          </h2>
          <button onClick={props.onClose} className="rounded-lg p-1.5 text-muted hover:text-fg" aria-label="閉じる"><X className="size-5" aria-hidden /></button>
        </header>
        <div className="flex min-h-0 flex-1">
          <nav className={`${category ? 'hidden' : 'flex'} w-full flex-col gap-0.5 p-2 md:flex md:w-48 md:shrink-0 md:border-r md:border-border`}>
            {CATEGORIES.map((item) => (
              <button key={item.id} onClick={() => setCategory(item.id)} aria-current={category === item.id ? 'page' : undefined}
                className={`flex items-center gap-2 rounded-lg px-3 py-2.5 text-left text-sm md:py-2 ${category === item.id ? 'bg-raised font-medium' : 'hover:bg-surface'}`}>
                <item.icon className="size-4 text-muted" aria-hidden />
                <span className="flex-1">{item.label}</span>
                <ChevronRight className="size-4 text-muted md:hidden" aria-hidden />
              </button>
            ))}
          </nav>
          {category && (
            <section className="min-w-0 flex-1 overflow-y-auto p-4">
              {category === 'accounts' && <AccountsPane executors={props.executors} onChanged={props.onChanged} />}
              {category === 'permissions' && <PermissionsPane onChanged={props.onChanged} />}
              {category === 'server' && <ServerPane />}
              {category === 'sessions' && <SessionsPane onDiscover={props.onDiscover} onDeletedAll={props.onDeletedAll} />}
            </section>
          )}
        </div>
      </div>
    </div>
  )
}

function Card({ title, children, aside }: { title: string; children: React.ReactNode; aside?: React.ReactNode }) {
  return (
    <div className="mb-4 rounded-xl border border-border p-3">
      <div className="mb-2 flex items-center gap-2">
        <h3 className="flex-1 text-sm font-semibold">{title}</h3>
        {aside}
      </div>
      <div className="space-y-2 text-sm">{children}</div>
    </div>
  )
}

function Status({ ok, children }: { ok: boolean; children: React.ReactNode }) {
  return <span className={`inline-flex items-center gap-1.5 ${ok ? 'text-ok' : 'text-muted'}`}>
    <span className={`size-2 rounded-full ${ok ? 'bg-ok' : 'bg-muted'}`} aria-hidden />{children}
  </span>
}

function Usage({ usage }: { usage?: SubscriptionUsage | null }) {
  if (!usage?.windows.length) return null
  return <div className="space-y-0.5 text-xs text-muted">{usage.windows.map((window) => <UsageRow key={window.label} window={window} />)}</div>
}

function Problem({ text }: { text: string | null }) {
  return text ? <p role="alert" className="text-sm text-danger">{text}</p> : null
}

function Loading() {
  return <p className="text-sm text-muted">読み込み中…</p>
}

// --- アカウント・CLI ---------------------------------------------------------------------------------------

function AccountsPane({ executors, onChanged }: { executors: Executor[]; onChanged: () => void }) {
  const [data, setData] = useState<Accounts | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback((refresh = false) => {
    api.accounts(refresh).then((next) => { setData(next); setError(null) }).catch((caught) => setError(failure(caught)))
  }, [])
  useEffect(() => load(), [load])

  // While a Codex sign-in waits for the code to be entered, check every few seconds
  const waiting = !!data?.codex.login && !data.codex.login.error
  useEffect(() => {
    if (!waiting) return
    const timer = window.setInterval(() => load(true), 3000)
    return () => window.clearInterval(timer)
  }, [waiting, load])
  const signedIn = data?.codex.signed_in
  const previous = useRef(signedIn)
  useEffect(() => {
    if (previous.current !== undefined && previous.current !== signedIn) onChanged()
    previous.current = signedIn
  }, [signedIn, onChanged])

  async function act(action: () => Promise<unknown>) {
    setBusy(true)
    setError(null)
    try {
      await action()
      load(true)
    } catch (caught) {
      setError(failure(caught))
    } finally {
      setBusy(false)
    }
  }

  if (!data) return error ? <Problem text={error} /> : <Loading />
  const { codex, claude } = data
  const others = executors.filter((item) => item.kind !== 'subscription')
  return (
    <div>
      <Problem text={error} />
      <Card title="Codex（ChatGPT）" aside={<button onClick={() => load(true)} className="rounded p-1 text-muted hover:text-fg" aria-label="状態を更新" title="状態を更新"><RefreshCw className="size-4" aria-hidden /></button>}>
        {!codex.installed ? <p className="text-muted">Codex CLI がインストールされていません</p> : <>
          <p><Status ok={codex.signed_in}>{codex.signed_in ? 'ログイン済み' : '未ログイン'}</Status>
            {codex.plan && <span className="ml-2 text-muted">{String(codex.plan).toUpperCase()}</span>}</p>
          {codex.email && <p className="text-muted">{codex.email}{codex.workspace ? `（${codex.workspace}）` : ''}</p>}
          <Usage usage={codex.usage} />
          {codex.error && <p className="text-danger">{codex.error}</p>}
          {codex.login ? (
            <div className="rounded-lg bg-surface p-3">
              {codex.login.error ? <p className="text-danger">{codex.login.error}</p> : <>
                <p>次のページを開き、このコードを入力してください。</p>
                <p className="my-2 select-all font-mono text-2xl tracking-widest">{codex.login.code}</p>
                {codex.login.url && <a href={codex.login.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-accent underline">
                  {codex.login.url}<ExternalLink className="size-3.5" aria-hidden /></a>}
                <p className="mt-2 text-xs text-muted">入力が終わると、自動でログイン済みになります。</p>
              </>}
              <button onClick={() => act(api.codexCancelLogin)} disabled={busy} className={`${button} mt-2`}>{codex.login.error ? '閉じる' : 'キャンセル'}</button>
            </div>
          ) : codex.signed_in ? (
            <button onClick={() => { if (window.confirm('Codex からログアウトしますか？端末の codex も未ログインになります')) act(api.codexLogout) }}
              disabled={busy} className={button}>ログアウト</button>
          ) : (
            <button onClick={() => act(api.codexLogin)} disabled={busy} className={button}>ログイン</button>
          )}
        </>}
      </Card>

      <Card title="Claude Code（サブスクリプション）">
        {!claude.allowed ? (
          <p className="text-muted">無効です。.env の OMNI_ALLOW_CLAUDE_SUBSCRIPTION=true と、「権限」で有効にすると使えます。</p>
        ) : <>
          <p><Status ok={claude.signed_in}>{claude.signed_in ? 'ログイン済み' : '未ログイン'}</Status>
            {claude.plan && <span className="ml-2 text-muted">{claude.plan}</span>}</p>
          {claude.email && <p className="text-muted">{claude.email}</p>}
          <Usage usage={claude.usage} />
          {claude.error && <p className="text-danger">{claude.error}</p>}
          <p className="text-xs text-muted">ログイン・ログアウトはサーバーの端末で行います（<code>claude auth login</code> / <code>claude auth logout</code>）。規約により、アプリは Claude.ai の資格情報を扱いません。</p>
        </>}
      </Card>

      <Card title="API・ローカルLLM">
        {others.length === 0 ? <p className="text-muted">ありません</p> : (
          <ul className="divide-y divide-border">
            {others.map((item) => (
              <li key={item.id} className="flex items-center gap-2 py-1.5">
                <span className="min-w-0 flex-1 truncate">{item.label}</span>
                {item.configured ? <Status ok>設定済み</Status> : <span className="truncate text-xs text-muted">未設定: {item.setting}</span>}
              </li>
            ))}
          </ul>
        )}
        <p className="text-xs text-muted">APIキーとURLは .env で設定します。</p>
      </Card>

      <ModelDefaults executors={executors} defaults={data.model_defaults} onSaved={(defaults) => { setData({ ...data, model_defaults: defaults }); onChanged() }} />
    </div>
  )
}

const EFFORT_LABEL: Record<string, string> = { low: '低', medium: '中', high: '高', xhigh: '最高', max: '最大' }

function ModelDefaults({ executors, defaults, onSaved }: {
  executors: Executor[]
  defaults: Accounts['model_defaults']
  onSaved: (defaults: Accounts['model_defaults']) => void
}) {
  const [error, setError] = useState<string | null>(null)
  const usable = executors.filter((item) => item.configured && item.models.length > 0)

  function save(executor: string, model: string | null, effort: string | null) {
    setError(null)
    api.setModelDefault(executor, model, effort).then(onSaved).catch((caught) => setError(failure(caught)))
  }

  return (
    <Card title="新しい会話の既定のモデル">
      <p className="text-xs text-muted">実行元ごとに、新しい会話で最初に選ばれるモデルと思考の深さです。CLIの設定ファイルは変えません。</p>
      <Problem text={error} />
      {usable.length === 0 && <p className="text-muted">使える実行元がありません</p>}
      <ul className="divide-y divide-border">
        {usable.map((executor) => {
          const saved = defaults[executor.id]
          const model = executor.models.find((item) => item.id === saved?.model)
          return (
            <li key={executor.id} className="flex flex-wrap items-center gap-2 py-2">
              <span className="w-full truncate sm:w-44">{executor.label}</span>
              <select value={model ? model.id : ''} aria-label={`${executor.label}の既定のモデル`} className={`${select} min-w-0 flex-1`}
                onChange={(event) => save(executor.id, event.target.value || null, null)}>
                <option value="">指定しない（一覧の先頭）</option>
                {executor.models.map((item) => <option key={item.id} value={item.id}>{item.name === item.id ? item.id : item.name}</option>)}
              </select>
              {model && model.efforts.length > 0 && (
                <select value={saved?.effort ?? ''} aria-label={`${executor.label}の既定の思考の深さ`} className={select}
                  onChange={(event) => save(executor.id, model.id, event.target.value || null)}>
                  <option value="">深さ: 既定</option>
                  {model.efforts.map((effort) => <option key={effort} value={effort}>深さ: {EFFORT_LABEL[effort] ?? effort}</option>)}
                </select>
              )}
            </li>
          )
        })}
      </ul>
    </Card>
  )
}

// --- 権限 --------------------------------------------------------------------------------------------------

type Field = keyof PermissionValues
const APPROVAL: [ApprovalMode, string][] = [['ask', 'すべて確認'], ['auto_edit', '編集は自動'], ['auto', 'すべて自動']]
const OUTSIDE: [OutsideLimit, string][] = [['deny', '常に拒否'], ['ask', '常に確認'], ['mode', '承認モードに従う']]
const SWITCH: [boolean, string][] = [[false, '無効'], [true, '有効']]

const GROUPS: { title: string; note?: string; fields: { field: Field; label: string; help?: string; options: [string | boolean, string][] }[] }[] = [
  { title: '手動の端末', note: 'サーバーユーザーのシェルを直接操作します。エージェント用の承認・コマンド拒否・ディレクトリ・ネットワーク制限とは独立した許可です。',
    fields: [{ field: 'omni_allow_terminal', label: '対話型端末', help: '.env で許可した場合だけ有効にできます。無効にすると接続中の端末も終了します。', options: SWITCH }] },
  {
    title: 'Code の操作',
    fields: [
      { field: 'omni_approval_max', label: '承認モードの上限', help: '会話で選べる一番緩いモード', options: APPROVAL },
      { field: 'omni_approval_default', label: '承認モードの既定', help: '新しい会話の承認モード', options: APPROVAL },
      { field: 'omni_allow_shell', label: 'コマンドの実行', help: '無効にすると、コマンドは常に拒否', options: SWITCH },
      { field: 'omni_allow_network', label: 'エージェントのネットワーク接続', help: 'パッケージのインストールや git pull など', options: SWITCH },
    ],
  },
  {
    title: 'ディレクトリ',
    note: 'プロジェクト外の読み取りは、エージェントが確認を求める操作に適用されます。コマンドを使った読み取りは「コマンドの実行」と拒否するコマンドで制御します。',
    fields: [
      { field: 'omni_outside_read', label: 'プロジェクト外の読み取り', options: OUTSIDE },
      { field: 'omni_outside_write', label: 'プロジェクト外の編集・コマンド', options: OUTSIDE },
      { field: 'omni_allow_project_create', label: '画面からのフォルダ作成', options: SWITCH },
      { field: 'omni_allow_git_clone', label: '画面からの git clone', options: SWITCH },
    ],
  },
  {
    title: '実行元',
    fields: [
      { field: 'omni_allow_native_session_changes', label: 'CLI側セッションの改名・分岐・削除', options: SWITCH },
      { field: 'omni_allow_claude_subscription', label: 'Claude のサブスクリプション', help: '個人利用・自己責任', options: SWITCH },
      { field: 'omni_allow_opencode_free', label: 'OpenCode の無料モデル', help: 'コードが OpenCode のサービスに送られます', options: SWITCH },
    ],
  },
]

/** Options are ordered strictest first: anything after the .env value is looser and can't be chosen. */
function looser(options: [string | boolean, string][], value: string | boolean, limit: string | boolean) {
  const index = (item: string | boolean) => options.findIndex(([option]) => option === item)
  return index(value) > index(limit)
}

function PermissionsPane({ onChanged }: { onChanged: () => void }) {
  const [data, setData] = useState<Permissions | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pattern, setPattern] = useState('')

  useEffect(() => {
    api.permissions().then(setData).catch((caught) => setError(failure(caught)))
  }, [])

  function save(changes: Record<string, unknown>) {
    setError(null)
    api.setPermissions(changes).then((next) => { setData(next); onChanged() }).catch((caught) => setError(failure(caught)))
  }

  if (!data) return error ? <Problem text={error} /> : <Loading />
  const label = (options: [string | boolean, string][], value: string | boolean) => options.find(([option]) => option === value)?.[1] ?? String(value)
  return (
    <div>
      <p className="mb-3 text-xs text-muted">.env の値が上限です。ここでは、それより厳しくだけできます。変更はすぐに反映されます。</p>
      <Problem text={error} />
      {GROUPS.filter((group) => group.fields.some(({ field }) => field in data.values)).map((group) => (
        <Card key={group.title} title={group.title}>
          {group.note && <p className="text-xs text-muted">{group.note}</p>}
          <ul className="divide-y divide-border">
            {group.fields.filter(({ field }) => field in data.values).map(({ field, label: name, help, options }) => {
              const value = data.values[field]
              const limit = data.limits[field]
              const changed = data.changed.includes(field)
              return (
                <li key={field} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2">
                  <div className="min-w-0 flex-1 basis-48">
                    <p>{name}{changed && <span className="ml-2 text-xs text-accent">変更済み</span>}</p>
                    <p className="text-xs text-muted">{help ? `${help}。` : ''}上限: {label(options, limit)}</p>
                  </div>
                  <select value={String(value)} aria-label={name} className={select}
                    onChange={(event) => {
                      const raw = event.target.value
                      save({ [field]: raw === 'true' ? true : raw === 'false' ? false : raw })
                    }}>
                    {options.map(([option, text]) => (
                      <option key={String(option)} value={String(option)} disabled={looser(options, option, limit)}>
                        {text}{looser(options, option, limit) ? '（上限を超えます）' : ''}
                      </option>
                    ))}
                  </select>
                  {changed && <button onClick={() => save({ [field]: null })} className="text-xs text-muted underline">.env の値に戻す</button>}
                </li>
              )
            })}
          </ul>
        </Card>
      ))}
      <Card title="常に拒否するコマンド">
        <p className="text-xs text-muted">含まれていたら、承認モードに関係なく拒否します。.env の項目は外せません。</p>
        <div className="flex flex-wrap gap-1.5">
          {data.shell_deny_env.map((item) => <span key={item} className="rounded-md bg-raised px-2 py-0.5 font-mono text-xs" title=".env で設定">{item}</span>)}
          {data.shell_deny_extra.map((item) => (
            <span key={item} className="inline-flex items-center gap-1 rounded-md border border-accent px-2 py-0.5 font-mono text-xs">
              {item}
              <button onClick={() => save({ omni_shell_deny_extra: data.shell_deny_extra.filter((other) => other !== item) })}
                aria-label={`${item}を外す`} className="text-muted hover:text-danger"><X className="size-3" aria-hidden /></button>
            </span>
          ))}
        </div>
        <form className="flex gap-2" onSubmit={(event) => {
          event.preventDefault()
          if (pattern.trim()) save({ omni_shell_deny_extra: [...data.shell_deny_extra, pattern.trim()] })
          setPattern('')
        }}>
          <input value={pattern} onChange={(event) => setPattern(event.target.value)} placeholder="例: npm publish" aria-label="拒否するコマンドを追加"
            className="min-w-0 flex-1 rounded-lg border border-border bg-surface px-3 py-1.5 font-mono text-sm outline-none focus:border-accent" />
          <button type="submit" disabled={!pattern.trim() || pattern.includes(',')} className={button}>追加</button>
        </form>
      </Card>
      <Card title="プロジェクトの置き場所">
        <p className="break-all font-mono text-xs">{data.projects_dir}</p>
        <p className="text-xs text-muted">この直下のフォルダが、Code のプロジェクトになります。変更は .env の OMNI_PROJECTS_DIR で行います。</p>
      </Card>
    </div>
  )
}

// --- サーバー ------------------------------------------------------------------------------------------------

function duration(seconds: number): string {
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  return days ? `${days}日${hours}時間` : hours ? `${hours}時間${minutes}分` : `${minutes}分`
}

function bytes(value: number): string {
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KB`
  if (value < 1024 ** 3) return `${(value / 1024 / 1024).toFixed(1)} MB`
  return `${(value / 1024 ** 3).toFixed(2)} GB`
}

function ServerPane() {
  const [data, setData] = useState<ServerStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [restarting, setRestarting] = useState(false)

  const load = useCallback(() => {
    api.server().then((next) => { setData(next); setError(null) }).catch((caught) => setError(failure(caught)))
  }, [])
  useEffect(load, [load])

  async function restart() {
    if (!data || !window.confirm('サーバーを再起動しますか？数秒間つながらなくなります')) return
    setError(null)
    try {
      const { started_at } = await api.restartServer()
      setRestarting(true)
      // Wait until a new process answers, then load the page again so the screen matches the server
      const until = Date.now() + 60_000
      while (Date.now() < until) {
        await new Promise((resolve) => window.setTimeout(resolve, 1500))
        try {
          const next = await api.server()
          if (next.started_at !== started_at) {
            window.location.reload()
            return
          }
        } catch {
          /* still restarting */
        }
      }
      setRestarting(false)
      setError('再起動を確認できませんでした。サーバーの状態を確認してください')
    } catch (caught) {
      setError(failure(caught))
    }
  }

  if (!data) return error ? <Problem text={error} /> : <Loading />
  const rows: [string, React.ReactNode][] = [
    ['状態', <Status ok>稼働中（{duration(data.uptime_seconds)}）</Status>],
    ['実行中の返答', `${data.active_runs}件`],
    ['接続中の端末', `${data.active_terminals ?? 0}件`],
    ['バージョン', `${data.version}（Python ${data.python}）`],
    ['起動方法', data.systemd ? 'systemd' : '手動'],
    ['待ち受け', data.listen],
    ['公開URL', data.public_urls.length ? data.public_urls.join(', ') : '未設定'],
    ['データ', `${bytes(data.data_bytes)}　${data.data_dir}`],
    ['プロジェクト', `${data.projects}件　${data.projects_dir}`],
  ]
  return (
    <div>
      <Problem text={error} />
      <Card title="サーバー" aside={<button onClick={load} className="rounded p-1 text-muted hover:text-fg" aria-label="状態を更新" title="状態を更新"><RefreshCw className="size-4" aria-hidden /></button>}>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5">
          {rows.map(([name, value]) => <div key={name} className="contents"><dt className="text-muted">{name}</dt><dd className="min-w-0 break-all">{value}</dd></div>)}
        </dl>
        {data.restart_required && <p className="text-warn">画面が更新されています。再起動すると反映されます。</p>}
      </Card>
      <Card title="エージェント">
        <ul className="divide-y divide-border">
          {data.agents.map((agent) => (
            <li key={agent.id} className="flex items-center gap-2 py-1.5">
              <span className="w-28 shrink-0">{agent.label}</span>
              <Status ok={agent.installed}>{agent.installed ? (agent.version ?? 'インストール済み') : '未インストール'}</Status>
            </li>
          ))}
        </ul>
      </Card>
      <Card title="再起動">
        <p className="text-xs text-muted">設定やプログラムの更新を反映します。返答・端末を終了してから再起動してください。</p>
        {!data.can_restart && <p className="text-xs text-muted">この起動方法では、画面から再起動できません。</p>}
        <button onClick={restart} disabled={restarting || data.active_runs > 0 || (data.active_terminals ?? 0) > 0 || !data.can_restart} className={button}>
          {restarting ? '再起動中…' : data.active_runs > 0 ? `実行中の返答があります（${data.active_runs}件）` : (data.active_terminals ?? 0) > 0 ? '接続中の端末があります' : 'サーバーを再起動'}
        </button>
      </Card>
    </div>
  )
}

// --- セッション ------------------------------------------------------------------------------------------------

function SessionsPane({ onDiscover, onDeletedAll }: { onDiscover: () => void; onDeletedAll: () => void }) {
  const [data, setData] = useState<SessionCounts | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [confirm, setConfirm] = useState('')
  const [result, setResult] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    api.sessionCounts().then(setData).catch((caught) => setError(failure(caught)))
  }, [])
  useEffect(load, [load])

  async function removeAll() {
    if (!data) return
    setBusy(true)
    setError(null)
    try {
      const { deleted } = await api.deleteAllConversations(confirm)
      setResult(`${deleted}件の会話を削除しました`)
      setConfirm('')
      onDeletedAll()
      load()
    } catch (caught) {
      setError(failure(caught))
    } finally {
      setBusy(false)
    }
  }

  if (!data) return error ? <Problem text={error} /> : <Loading />
  return (
    <div>
      <Problem text={error} />
      <Card title="アプリの会話">
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5">
          <dt className="text-muted">会話</dt><dd>{data.conversations}件（Chat {data.chat}・Code {data.code}）</dd>
          <dt className="text-muted">非表示</dt><dd>{data.hidden}件</dd>
          <dt className="text-muted">CLIから取り込み</dt><dd>Codex {data.imported.codex}件・Claude Code {data.imported.claude}件</dd>
          <dt className="text-muted">取り込まない</dt><dd>{data.skipped}件（アプリで削除したCLIの会話）</dd>
        </dl>
      </Card>
      <Card title="CLIの会話の取り込み">
        <p className="text-xs text-muted">各プロジェクトで Codex・Claude Code を使った会話を探して、一覧に加えます。画面を開いたときにも自動で行います。</p>
        <button onClick={() => { onDiscover(); window.setTimeout(load, 3000) }} className={button}>今すぐ取り込む</button>
      </Card>
      <Card title="すべての会話を削除">
        <p className="text-xs text-muted">アプリのすべての会話と履歴を削除します。元に戻せません。CLI側の会話は残りますが、削除したものは再び取り込みません。</p>
        <label className="block text-xs">確認のため「{data.confirm_word}」と入力してください
          <input value={confirm} onChange={(event) => setConfirm(event.target.value)} aria-label="確認の入力"
            className="mt-1 block w-full max-w-60 rounded-lg border border-border bg-surface px-3 py-1.5 text-sm outline-none focus:border-danger" />
        </label>
        <button onClick={removeAll} disabled={busy || confirm !== data.confirm_word || data.active_runs > 0 || data.conversations === 0}
          className="rounded-lg bg-danger px-3 py-1.5 text-sm font-medium text-white disabled:opacity-40">
          {data.active_runs > 0 ? '返答の実行中は削除できません' : `${data.conversations}件の会話を削除`}
        </button>
        {result && <p role="status" className="text-ok">{result}</p>}
      </Card>
    </div>
  )
}
