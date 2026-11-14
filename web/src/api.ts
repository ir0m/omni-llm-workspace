import type { PublicKeyCredentialCreationOptionsJSON, PublicKeyCredentialRequestOptionsJSON } from '@simplewebauthn/browser'

export type PlanEntry = { content: string; status: 'pending' | 'in_progress' | 'completed'; priority?: string }

export type ToolCall = {
  type: 'tool_call'
  id: string
  title: string
  kind: string
  status: string
  locations?: { path: string }[]
  command?: string
  path?: string
  diff?: string
  output?: string
  note?: string
}

export type Block =
  | { type: 'text' | 'thinking' | 'user_input'; text: string }
  | ToolCall
  | { type: 'plan'; entries: PlanEntry[] }
  | { type: 'handover'; agent: string; count: number }

export type Usage = {
  inputTokens?: number
  outputTokens?: number
  cacheReadTokens?: number
  cacheWriteTokens?: number
  reasoningTokens?: number
  costUsd?: number
  contextWindow?: number
  contextUsed?: number
  contextBreakdown?: { name: string; tokens: number }[]
  stopReason?: string
}

export type ApprovalMode = 'ask' | 'auto_edit' | 'auto'

export type PermissionAsk = {
  requestId: string
  tool_call_id: string
  kind: string
  title: string
  command: string | null
  paths: string[]
  diff: string | null
  outside_project: boolean
}

export type AppConfig = {
  capabilities?: string[]
  restart_required?: boolean
  approval_default: ApprovalMode
  approval_max: ApprovalMode
  allow_shell: boolean
  shell_deny: string[]
  allow_opencode_free: boolean
  allow_claude_subscription: boolean
  allow_mcp?: boolean
  allow_file_edit?: boolean
  allow_compile?: boolean
  compilers?: { typst: boolean; tex: boolean }
}

export type ErrorInfo = { code: string; message: string }

export type Me = { signed_in: boolean; registered: boolean; password: boolean; passkey_origins: string[]; passkey_id: number | null }
export type PasskeyInfo = { id: number; name: string; rp_id: string; created_at: number; last_used_at: number | null; current: boolean }

export type Message = {
  id: string
  seq: number
  role: 'user' | 'assistant'
  content: Block[]
  provider: string | null
  model: string | null
  agent: string | null
  status: 'complete' | 'streaming' | 'error' | 'cancelled' | 'interrupted'
  error: ErrorInfo | null
  usage: Usage | null
  created_at: number
}

export type Target = { provider: string; model: string; effort?: string | null }

export type Mode = 'chat' | 'code'

export type Conversation = {
  has_native_session?: boolean
  id: string
  title: string
  mode: Mode
  project: string | null
  temporary: boolean
  pinned: boolean
  hidden: boolean
  forked_from: string | null
  target: Target | null
  approval_mode: ApprovalMode | null
  allow_rules: { kind: string; prefix?: string; outside?: boolean; tool?: string }[]
  /** MCP servers this conversation doesn't use */
  mcp_off?: string[]
  created_at: number
  updated_at: number
  active_run: string | null
}

export type PendingInput = { id: string; run_id: string; text: string; status: 'queued' | 'returned' }
export type ContextSummary = { text: string; through_seq: number; source_chars: number; usage: { inputTokens: number; outputTokens: number } }
export type ConversationDetail = Conversation & { context?: ContextSummary | null; messages: Message[]; sync_error?: string | null; pending_inputs?: PendingInput[] }

export type ModelInfo = {
  id: string
  name: string
  context_window: number | null
  max_output: number | null
  vision: boolean | null
  reasoning: boolean | null
  efforts: string[]
}

/** Something that can take a turn: a provider (API, local) or an agent (subscription CLIs, OpenCode). */
export type Executor = {
  id: string
  label: string
  kind: 'api' | 'local' | 'subscription'
  configured: boolean
  setting: string | null
  error: ErrorInfo | null
  modes: Mode[]
  code_via: string | null
  note?: string | null
  models: ModelInfo[]
  usage?: SubscriptionUsage | null
  /** The model and effort a new conversation starts with (settings screen) */
  default?: { model: string; effort: string | null } | null
}

export type UsageWindow = {
  label: string
  usedPercent: number | null
  resetsAt: number | null
  status?: 'allowed' | 'allowed_warning' | 'rejected' | null
  at?: number | null
}
export type SubscriptionUsage = { plan: string | null; windows: UsageWindow[] }

export type Project = {
  name: string
  path: string
  git: { branch: string | null; changes: number | null } | null
  modified_at: number
}

export type ProjectList = {
  root: string
  can_create: boolean
  can_clone: boolean
  projects: Project[]
  /** Folders that only hold projects, as "work" and "work/client" */
  groups?: string[]
  max_depth?: number
}

/** A project name in a URL path: "work/client/app" keeps its slashes, each part encoded. */
export function projectPath(name: string): string {
  return name.split('/').map(encodeURIComponent).join('/')
}

export type FileEntry = { name: string; path: string; kind: 'directory' | 'file' | 'link' | 'other'; size: number }
export type FileTree = { path: string; entries: FileEntry[]; next_offset: number | null }
export type FilePreview = { path: string; size: number } & (
  | { kind: 'text'; text: string; truncated: boolean; hash?: string | null }
  | { kind: 'image'; media_type: string; too_large: boolean }
  | { kind: 'pdf'; too_large: boolean }
  | { kind: 'binary' }
)
export type SavedFile = { path: string; size: number; hash: string }
export type Diagnostic = { path: string | null; line: number | null; severity: 'error' | 'warning'; message: string }
export type CompileResult = { ok: boolean; engine: string; seconds: number; log: string; diagnostics: Diagnostic[]; has_pdf: boolean }
export type NoteSummary = { path: string; title: string; aliases: string[]; tags: string[]; headings: string[] }
export type NoteIndex = { notes: NoteSummary[]; paths: string[]; backlinks: { path: string; title: string }[]
  results: { path: string; title: string; line: number; snippet: string }[]; truncated: boolean }
export type ChangeStage = 'staged' | 'unstaged' | 'untracked'
export type GitChange = { path: string; old_path: string | null; status: string }
export type ProjectChanges = { is_git: boolean } & Record<ChangeStage, GitChange[]>
export type FileDiff = { path: string; patch: string; truncated: boolean }
export type Worktree = { name: string; source: string; title: string; branch: string; base: string; conversation_id: string; conversation: Conversation | null }
export type WorktreeResult = { changes: ProjectChanges; paths: string[]; answer: string }
export type ProjectBranches = { branches: { ref: string; name: string; remote: boolean }[]; current_ref: string | null; head: string | null }

export type OutsideLimit = 'mode' | 'ask' | 'deny'
export type PermissionValues = {
  omni_allow_native_session_changes: boolean
  omni_allow_terminal: boolean
  omni_allow_file_edit: boolean
  omni_allow_compile: boolean
  omni_approval_default: ApprovalMode
  omni_approval_max: ApprovalMode
  omni_outside_read: OutsideLimit
  omni_outside_write: OutsideLimit
  omni_allow_shell: boolean
  omni_allow_network: boolean
  omni_allow_project_create: boolean
  omni_allow_git_clone: boolean
  omni_allow_claude_subscription: boolean
  omni_allow_opencode_free: boolean
  omni_allow_agy: boolean
  omni_allow_mcp: boolean
  omni_allow_mcp_stdio: boolean
}
export type Permissions = {
  values: PermissionValues
  limits: PermissionValues
  changed: string[]
  shell_deny_env: string[]
  shell_deny_extra: string[]
  projects_dir: string
}
export type CodexLogin = { id: string | null; code: string | null; url: string | null; error: string | null; done: boolean }
export type Accounts = {
  codex: { installed: boolean; signed_in: boolean; error: string | null; login: CodexLogin | null; kind?: string | null
    email?: string | null; plan?: string | null; workspace?: string | null; usage?: SubscriptionUsage | null }
  claude: { allowed: boolean; signed_in: boolean; error: string | null; plan?: string | null; email?: string | null
    usage?: SubscriptionUsage | null }
  model_defaults: Record<string, { model: string; effort: string | null }>
}
export type ServerStatus = {
  active_terminals?: number
  version: string
  python: string
  pid: number
  started_at: number
  uptime_seconds: number
  systemd: boolean
  can_restart: boolean
  restart_required: boolean
  active_runs: number
  listen: string
  public_urls: string[]
  data_dir: string
  data_bytes: number
  projects_dir: string
  projects: number
  agents: { id: string; label: string; installed: boolean; version: string | null }[]
}
export type SessionCounts = {
  conversations: number
  chat: number
  code: number
  hidden: number
  imported: { codex: number; claude: number }
  skipped: number
  active_runs: number
  confirm_word: string
}

export type McpTool = { name: string; description: string; read_only: boolean }
export type McpServer = {
  id: string
  name: string
  transport: 'stdio' | 'http'
  command: string
  args: string[]
  url: string
  env_keys: string[]
  header_keys: string[]
  enabled: boolean
  always_allow: string[]
  tools: McpTool[]
  usable: boolean
}
export type McpSettings = { allowed: boolean; allow_stdio: boolean; servers: McpServer[] }
/** A server from the form. Secrets: a string sets the value, null removes it, a missing key keeps it. */
export type McpServerInput = {
  name?: string
  transport?: 'stdio' | 'http'
  command?: string
  args?: string[]
  url?: string
  env?: Record<string, string | null>
  headers?: Record<string, string | null>
  enabled?: boolean
  always_allow?: string[]
}

export type Draft = { temporary: boolean; mode: Mode; project: string | null; approval_mode?: ApprovalMode | null; mcp_off?: string[] }

const DETAIL_TEXT: Record<string, string> = {
  conversation_not_found: 'この会話は見つかりません（削除された可能性があります）',
  message_not_found: 'メッセージが見つかりません',
  project_not_found: 'プロジェクトが見つかりません',
  agent_not_installed: 'このエージェントはインストールされていません',
  provider_not_configured: 'この実行元は設定されていません',
  code_needs_agent: 'Code ではエージェントを選んでください',
  reply_in_progress: '返答の途中です。終わってから送ってください',
  run_not_found: 'この実行は終了しています',
  request_not_found: 'この承認はすでに締め切られています',
  not_signed_in: 'ログインしてください',
}

export class ApiError extends Error {
  status: number
  detail: string
  constructor(status: number, detail: string) {
    // 405, or FastAPI's bare 404 for an unknown route: the screen is newer than the running server
    const outdated = status === 405 || (status === 404 && detail === 'Not Found')
    super(outdated ? 'サーバーがこの操作に対応していません。更新後はサーバーを再起動してください。' : DETAIL_TEXT[detail] ?? detail)
    this.status = status
    this.detail = detail
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!response.ok) {
    let detail: unknown = response.statusText
    try {
      detail = (await response.json()).detail ?? detail
    } catch {
      /* not JSON */
    }
    const text = typeof detail === 'string' ? detail
      : detail && typeof detail === 'object' && 'message' in detail ? String((detail as { message: unknown }).message || JSON.stringify(detail))
      : JSON.stringify(detail)
    throw new ApiError(response.status, text)
  }
  return response.json() as Promise<T>
}

export const api = {
  compactContext: (id: string, target: Target) => request<ContextSummary>('POST', `/api/conversations/${id}/context`, target),
  compactNativeContext: (id: string, target: Target) => request<{ ok: boolean }>('POST', `/api/conversations/${id}/context/native`, target),
  clearContext: (id: string) => request<{ ok: boolean }>('DELETE', `/api/conversations/${id}/context`),
  branches: (project: string) => request<ProjectBranches>('GET', `/api/projects/${projectPath(project)}/branches`),
  worktrees: (project: string) => request<{ items: Worktree[]; can_create: boolean }>('GET', `/api/projects/${projectPath(project)}/worktrees`),
  createWorktree: (project: string, title: string, base: string) => request<Worktree>('POST', `/api/projects/${projectPath(project)}/worktrees`, { title, base }),
  worktreeResult: (project: string) => request<WorktreeResult>('GET', `/api/projects/${projectPath(project)}/worktree-result`),
  worktreeDiff: (project: string, path: string) => request<FileDiff>('GET', `/api/projects/${projectPath(project)}/worktree-diff?${new URLSearchParams({ path })}`),
  mergeWorktree: (project: string) => request<{ ok: boolean }>('POST', `/api/projects/${projectPath(project)}/worktree-merge`),
  removeWorktree: (project: string) => request<{ ok: boolean }>('DELETE', `/api/projects/${projectPath(project)}/worktree`),
  terminal: (project: string) => request<{ enabled: boolean }>('GET', `/api/projects/${projectPath(project)}/terminal`),
  me: () => request<Me>('GET', '/api/auth/me'),
  passkeyRegisterOptions: (token?: string) =>
    request<{ challenge_id: string; options: PublicKeyCredentialCreationOptionsJSON }>('POST', '/api/auth/passkey/register/options', { token: token ?? null }),
  passkeyRegisterVerify: (challengeId: string, credential: unknown, name: string) =>
    request<{ ok: boolean; id: number }>('POST', '/api/auth/passkey/register/verify', { challenge_id: challengeId, credential, name }),
  passkeyLoginOptions: () =>
    request<{ challenge_id: string; options: PublicKeyCredentialRequestOptionsJSON }>('POST', '/api/auth/passkey/login/options'),
  passkeyLoginVerify: (challengeId: string, credential: unknown) =>
    request<{ ok: boolean }>('POST', '/api/auth/passkey/login/verify', { challenge_id: challengeId, credential }),
  passkeys: () => request<PasskeyInfo[]>('GET', '/api/auth/passkeys'),
  deletePasskey: (id: number) => request<{ ok: boolean }>('DELETE', `/api/auth/passkeys/${id}`),
  login: (password: string) => request<{ ok: boolean }>('POST', '/api/auth/login', { password }),
  logout: () => request<{ ok: boolean }>('POST', '/api/auth/logout'),
  conversations: (includeHidden: boolean) =>
    request<Conversation[]>('GET', `/api/conversations?include_hidden=${includeHidden}`),
  permissions: () => request<Permissions>('GET', '/api/settings/permissions'),
  setPermissions: (changes: Record<string, unknown>) => request<Permissions>('PUT', '/api/settings/permissions', changes),
  accounts: (refresh = false) => request<Accounts>('GET', `/api/settings/accounts?refresh=${refresh}`),
  codexLogin: () => request<CodexLogin>('POST', '/api/settings/accounts/codex/login'),
  codexCancelLogin: () => request<{ ok: boolean }>('DELETE', '/api/settings/accounts/codex/login'),
  codexLogout: () => request<{ ok: boolean }>('POST', '/api/settings/accounts/codex/logout'),
  setModelDefault: (executor: string, model: string | null, effort: string | null) =>
    request<Accounts['model_defaults']>('PUT', '/api/settings/model-defaults', { executor, model, effort }),
  server: () => request<ServerStatus>('GET', '/api/settings/server'),
  restartServer: () => request<{ ok: boolean; started_at: number }>('POST', '/api/settings/server/restart'),
  health: () => request<{ status: string; version: string }>('GET', '/api/health'),
  sessionCounts: () => request<SessionCounts>('GET', '/api/settings/sessions'),
  deleteAllConversations: (confirm: string) =>
    request<{ deleted: number }>('POST', '/api/settings/conversations/delete-all', { confirm }),
  mcp: () => request<McpSettings>('GET', '/api/settings/mcp'),
  addMcp: (server: McpServerInput) => request<McpSettings>('POST', '/api/settings/mcp', server),
  changeMcp: (id: string, server: McpServerInput) => request<McpSettings>('PUT', `/api/settings/mcp/${encodeURIComponent(id)}`, server),
  removeMcp: (id: string) => request<McpSettings>('DELETE', `/api/settings/mcp/${encodeURIComponent(id)}`),
  inspectMcp: (id: string) => request<{ ok: boolean; error: string | null; tools: McpTool[] }>('POST', `/api/settings/mcp/${encodeURIComponent(id)}/inspect`),
  discoverSessions: () => request<{ added: number; errors: string[] }>('POST', '/api/sessions/discover'),
  conversation: (id: string) => request<ConversationDetail>('GET', `/api/conversations/${id}`),
  create: (draft: Draft) => request<Conversation>('POST', '/api/conversations', draft),
  update: (id: string, patch: Partial<Pick<Conversation, 'title' | 'pinned' | 'hidden' | 'temporary' | 'mode' | 'approval_mode' | 'mcp_off'>> & { project?: string; clear_allow_rules?: boolean }) =>
    request<Conversation>('PATCH', `/api/conversations/${id}`, patch),
  remove: (id: string, native = false) => request<{ ok: boolean }>('DELETE', `/api/conversations/${id}?native=${native}`),
  fork: (id: string, messageId?: string) =>
    request<Conversation>('POST', `/api/conversations/${id}/fork`, { message_id: messageId ?? null }),
  reorder: (ids: string[]) => request<{ ok: boolean }>('PUT', '/api/conversations/order', { ids }),
  send: (id: string, body: { text: string; provider: string; model: string; effort?: string | null }) =>
    request<{ run_id: string; user_message: Message; assistant_message: Message }>(
      'POST', `/api/conversations/${id}/messages`, body),
  cancel: (runId: string) => request<{ cancelled: boolean }>('POST', `/api/runs/${runId}/cancel`),
  input: (runId: string, id: string, text: string) =>
    request<{ id: string; status: 'delivered' | 'queued' | 'returned' }>('POST', `/api/runs/${runId}/input`, { id, text }),
  removeInput: (conversationId: string, id: string) =>
    request<{ ok: boolean; removed: boolean }>('DELETE', `/api/conversations/${conversationId}/inputs/${id}`),
  answer: (runId: string, requestId: string, outcome: 'allow' | 'allow_always' | 'deny', message?: string) =>
    request<{ ok: boolean }>('POST', `/api/runs/${runId}/permissions/${requestId}`, { outcome, message: message ?? null }),
  config: () => request<AppConfig>('GET', '/api/config'),
  executors: (refresh = false) => request<Executor[]>('GET', `/api/executors?refresh=${refresh}`),
  projects: () => request<ProjectList>('GET', '/api/projects'),
  files: (project: string, path = '', offset = 0, showHidden = true) => request<FileTree>('GET',
    `/api/projects/${projectPath(project)}/files?${new URLSearchParams({ path, offset: String(offset), show_hidden: String(showHidden) })}`),
  file: (project: string, path: string) => request<FilePreview>('GET',
    `/api/projects/${projectPath(project)}/file?${new URLSearchParams({ path })}`),
  /** The whole file (up to the editor's limit) with the hash a save must send back */
  fileFull: (project: string, path: string) => request<FilePreview>('GET',
    `/api/projects/${projectPath(project)}/file?${new URLSearchParams({ path, full: 'true' })}`),
  saveFile: (project: string, path: string, text: string, baseHash: string | null, create = false) => request<SavedFile>('PUT',
    `/api/projects/${projectPath(project)}/file`, { path, text, base_hash: baseHash, create }),
  deleteFile: (project: string, path: string) => request<{ path: string }>('DELETE',
    `/api/projects/${projectPath(project)}/file?${new URLSearchParams({ path })}`),
  makeFolder: (project: string, path: string) => request<{ path: string }>('POST', `/api/projects/${projectPath(project)}/folder`, { path }),
  moveFile: (project: string, path: string, to: string) => request<{ path: string }>('POST', `/api/projects/${projectPath(project)}/move`, { path, to }),
  imageUrl: (project: string, path: string, version: number) =>
    `/api/projects/${projectPath(project)}/image?${new URLSearchParams({ path, v: String(version) })}`,
  changes: (project: string) => request<ProjectChanges>('GET', `/api/projects/${projectPath(project)}/changes`),
  diff: (project: string, path: string, stage: ChangeStage) => request<FileDiff>('GET',
    `/api/projects/${projectPath(project)}/diff?${new URLSearchParams({ path, stage })}`),
  compile: (project: string, path: string) => request<CompileResult>('POST', `/api/projects/${projectPath(project)}/compile`, { path }),
  /** A PDF of the project, or the last build of a .typ / .tex document */
  pdfUrl: (project: string, path: string, version: number) =>
    `/api/projects/${projectPath(project)}/pdf?${new URLSearchParams({ path, v: String(version) })}`,
  notes: (project: string, path: string, q: string) => request<NoteIndex>('GET',
    `/api/projects/${projectPath(project)}/notes?${new URLSearchParams({ path, q })}`),
  createProject: (body: { name: string; git_init: boolean; readme: boolean; gitignore: boolean }) =>
    request<{ name: string }>('POST', '/api/projects', body),
  cloneProject: (body: { url: string; name?: string }) => request<{ name: string }>('POST', '/api/projects/clone', body),
}

export const errorText: Record<string, string> = {
  auth: '認証に失敗しました。.env のキーを確認してください',
  not_found: 'モデルが見つかりません',
  rate_limit: '利用上限に達しました',
  overloaded: 'サービスが混み合っています',
  bad_request: 'リクエストが受け付けられませんでした',
  network: '接続できませんでした',
  refused: '応答が拒否されました',
  internal: 'アプリ内でエラーが起きました',
  unknown: 'エラーが起きました',
}

export function textOf(message: Message | { content: Block[] }): string {
  return message.content.map((block) => (block.type === 'text' ? block.text : '')).join('')
}
