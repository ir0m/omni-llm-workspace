import { browserSupportsWebAuthn, startAuthentication, startRegistration, WebAuthnError } from '@simplewebauthn/browser'
import { api, ApiError, type Me } from './api'

export function deviceName(): string {
  const agent = navigator.userAgent
  if (/iPhone/.test(agent)) return 'iPhone'
  if (/iPad/.test(agent)) return 'iPad'
  if (/Android/.test(agent)) return 'Android'
  if (/Macintosh/.test(agent)) return 'Mac'
  if (/Windows/.test(agent)) return 'Windows'
  if (/Linux/.test(agent)) return 'Linux'
  return 'この端末'
}

/** Why passkeys can't be used on this page, or null when they can. */
export function passkeyProblem(me: Me | null): string | null {
  if (!window.isSecureContext || !browserSupportsWebAuthn()) {
    return 'パスキーは HTTPS（Tailscale Serve）か localhost で開いたときだけ使えます'
  }
  if (me && !me.passkey_origins.includes(window.location.origin)) {
    return `このURL（${window.location.origin}）は .env の OMNI_PUBLIC_URLS にありません`
  }
  return null
}

export function describeError(error: unknown): string {
  if (error instanceof WebAuthnError || (error instanceof Error && error.name === 'NotAllowedError')) {
    return 'パスキーの操作がキャンセルされたか、時間切れになりました'
  }
  if (error instanceof ApiError) {
    const text: Record<string, string> = {
      link_invalid: 'この登録リンクは使えません（期限切れか使用済み）',
      too_many_attempts: '試行回数が多すぎます。しばらく待ってください',
      sign_in_failed: 'このパスキーではログインできません',
      no_passkey: 'このURLで登録されたパスキーがありません',
      origin_not_allowed: 'このURLではパスキーを使えません',
      challenge_expired: '時間切れです。もう一度試してください',
    }
    return text[error.detail] ?? `失敗しました（${error.detail}）`
  }
  return '失敗しました'
}

export async function registerPasskey(name: string, token?: string): Promise<void> {
  const { challenge_id, options } = await api.passkeyRegisterOptions(token)
  const credential = await startRegistration({ optionsJSON: options })
  await api.passkeyRegisterVerify(challenge_id, credential, name)
}

export async function signInWithPasskey(): Promise<void> {
  const { challenge_id, options } = await api.passkeyLoginOptions()
  const credential = await startAuthentication({ optionsJSON: options })
  await api.passkeyLoginVerify(challenge_id, credential)
}
