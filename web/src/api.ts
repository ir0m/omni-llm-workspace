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
  | { type: 'text' | 'thinking'; text: string }
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
  approval_default: ApprovalMode
  approval_max: ApprovalMode
  allow_shell: boolean
  shell_deny: string[]
  allow_opencode_free: boolean
  allow_claude_subscription: boolean
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
  allow_rules: { kind: string; prefix?: string; outside?: boolean }[]
  created_at: number
  updated_at: number
  active_run: string | null
}

export type ConversationDetail = Conversation & { messages: Message[] }

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

export type ProjectList = { root: string; can_create: boolean; can_clone: boolean; projects: Project[] }

export type Draft = { temporary: boolean; mode: Mode; project: string | null; approval_mode?: ApprovalMode | null }

export class ApiError extends Error {
  status: number
  detail: string
  constructor(status: number, detail: string) {
    super(detail)
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
  conversation: (id: string) => request<ConversationDetail>('GET', `/api/conversations/${id}`),
  create: (draft: Draft) => request<Conversation>('POST', '/api/conversations', draft),
  update: (id: string, patch: Partial<Pick<Conversation, 'title' | 'pinned' | 'hidden' | 'temporary' | 'mode' | 'approval_mode'>> & { project?: string; clear_allow_rules?: boolean }) =>
    request<Conversation>('PATCH', `/api/conversations/${id}`, patch),
  remove: (id: string) => request<{ ok: boolean }>('DELETE', `/api/conversations/${id}`),
  fork: (id: string, messageId?: string) =>
    request<Conversation>('POST', `/api/conversations/${id}/fork`, { message_id: messageId ?? null }),
  reorder: (ids: string[]) => request<{ ok: boolean }>('PUT', '/api/conversations/order', { ids }),
  send: (id: string, body: { text: string; provider: string; model: string; effort?: string | null }) =>
    request<{ run_id: string; user_message: Message; assistant_message: Message }>(
      'POST', `/api/conversations/${id}/messages`, body),
  cancel: (runId: string) => request<{ cancelled: boolean }>('POST', `/api/runs/${runId}/cancel`),
  answer: (runId: string, requestId: string, outcome: 'allow' | 'allow_always' | 'deny', message?: string) =>
    request<{ ok: boolean }>('POST', `/api/runs/${runId}/permissions/${requestId}`, { outcome, message: message ?? null }),
  config: () => request<AppConfig>('GET', '/api/config'),
  executors: (refresh = false) => request<Executor[]>('GET', `/api/executors?refresh=${refresh}`),
  projects: () => request<ProjectList>('GET', '/api/projects'),
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
