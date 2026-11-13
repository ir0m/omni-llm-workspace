export type Block = { type: 'text' | 'thinking'; text: string }

export type Usage = {
  inputTokens?: number
  outputTokens?: number
  cacheReadTokens?: number
  cacheWriteTokens?: number
  reasoningTokens?: number
  costUsd?: number
  contextWindow?: number
  stopReason?: string
}

export type ErrorInfo = { code: string; message: string }

export type Message = {
  id: string
  seq: number
  role: 'user' | 'assistant'
  content: Block[]
  provider: string | null
  model: string | null
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
}

export type Project = {
  name: string
  path: string
  git: { branch: string | null; changes: number | null } | null
  modified_at: number
}

export type ProjectList = { root: string; can_create: boolean; can_clone: boolean; projects: Project[] }

export type Draft = { temporary: boolean; mode: Mode; project: string | null }

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
  me: () => request<{ password_set: boolean; signed_in: boolean }>('GET', '/api/auth/me'),
  login: (password: string) => request<{ ok: boolean }>('POST', '/api/auth/login', { password }),
  logout: () => request<{ ok: boolean }>('POST', '/api/auth/logout'),
  conversations: (includeHidden: boolean) =>
    request<Conversation[]>('GET', `/api/conversations?include_hidden=${includeHidden}`),
  conversation: (id: string) => request<ConversationDetail>('GET', `/api/conversations/${id}`),
  create: (draft: Draft) => request<Conversation>('POST', '/api/conversations', draft),
  update: (id: string, patch: Partial<Pick<Conversation, 'title' | 'pinned' | 'hidden' | 'temporary' | 'mode'>> & { project?: string }) =>
    request<Conversation>('PATCH', `/api/conversations/${id}`, patch),
  remove: (id: string) => request<{ ok: boolean }>('DELETE', `/api/conversations/${id}`),
  fork: (id: string, messageId?: string) =>
    request<Conversation>('POST', `/api/conversations/${id}/fork`, { message_id: messageId ?? null }),
  reorder: (ids: string[]) => request<{ ok: boolean }>('PUT', '/api/conversations/order', { ids }),
  send: (id: string, body: { text: string; provider: string; model: string; effort?: string | null }) =>
    request<{ run_id: string; user_message: Message; assistant_message: Message }>(
      'POST', `/api/conversations/${id}/messages`, body),
  cancel: (runId: string) => request<{ cancelled: boolean }>('POST', `/api/runs/${runId}/cancel`),
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

export function textOf(message: Message): string {
  return message.content.filter((block) => block.type === 'text').map((block) => block.text).join('')
}
