/**
 * Voice input with the browser's own speech recognition (Web Speech API). Chrome and Edge send the audio to
 * their vendor's service to transcribe it; Safari uses Apple's. Nothing goes through this app's server.
 * It needs a secure page (HTTPS or localhost) and the microphone permission.
 */
import { useCallback, useEffect, useRef, useState } from 'react'

type Alternative = { transcript: string }
type Result = { isFinal: boolean; length: number; [index: number]: Alternative }
type ResultEvent = { resultIndex: number; results: { length: number; [index: number]: Result } }
type ErrorEvent = { error: string }
type Recognition = {
  lang: string; continuous: boolean; interimResults: boolean
  onresult: ((event: ResultEvent) => void) | null
  onerror: ((event: ErrorEvent) => void) | null
  onend: (() => void) | null
  start: () => void; stop: () => void; abort: () => void
}
type RecognitionClass = new () => Recognition

function recognitionClass(): RecognitionClass | null {
  const scope = window as unknown as { SpeechRecognition?: RecognitionClass; webkitSpeechRecognition?: RecognitionClass }
  return scope.SpeechRecognition ?? scope.webkitSpeechRecognition ?? null
}

/** Why voice input can't be used here, or null when it can. */
export function dictationUnavailable(): string | null {
  if (!recognitionClass()) return 'このブラウザは音声入力に対応していません（Chrome・Edge・Safari で使えます）'
  if (!window.isSecureContext) return '音声入力は HTTPS で開いたときだけ使えます'
  return null
}

const ERRORS: Record<string, string> = {
  'not-allowed': 'マイクの使用が許可されていません。ブラウザの設定でこのサイトのマイクを許可してください',
  'service-not-allowed': 'このブラウザでは音声認識サービスを使えません',
  'audio-capture': 'マイクが見つかりません',
  network: '音声認識サービスに接続できません',
  'language-not-supported': 'この言語の音声認識に対応していません',
}

/** Languages offered for voice input. Japanese is the default: the browser's own language is often English. */
export const LANGUAGES = [
  { code: 'ja-JP', label: '日本語' }, { code: 'en-US', label: 'English' },
  { code: 'zh-CN', label: '中文' }, { code: 'ko-KR', label: '한국어' },
] as const
const LANGUAGE_KEY = 'omni.dictationLanguage'

export function savedLanguage(): string {
  try {
    const value = localStorage.getItem(LANGUAGE_KEY)
    if (value && LANGUAGES.some((item) => item.code === value)) return value
  } catch { /* storage disabled */ }
  return 'ja-JP'
}

export function saveLanguage(code: string) {
  try { localStorage.setItem(LANGUAGE_KEY, code) } catch { /* storage disabled */ }
}

// Android's Chrome repeats earlier words in each result of a continuous session, so it gets one utterance
// per session there, and sessions are chained instead.
const SINGLE_UTTERANCE = /Android/i.test(navigator.userAgent)
// Errors after which listening again would only fail the same way
const FATAL = new Set(['not-allowed', 'service-not-allowed', 'audio-capture', 'language-not-supported', 'network'])

/** Joins dictated text to what is already typed, with a space where both sides are words of a spaced language. */
export function joinDictation(before: string, spoken: string): string {
  if (!before || !spoken) return before + spoken
  const needsSpace = /[A-Za-z0-9.,!?;:]$/.test(before) && /^[A-Za-z0-9]/.test(spoken)
  return before + (needsSpace ? ' ' : '') + spoken
}

/**
 * Dictation into a text field. While listening, `onText` receives the field's text: what was there when
 * dictation started, then what was recognised so far (including words still being recognised).
 * Browsers end a session after a pause or a minute; it starts again until the user stops.
 */
export function useDictation({ text, language, onText, onError }: {
  text: string; language: string; onText: (value: string) => void; onError: (message: string) => void
}) {
  const [listening, setListening] = useState(false)
  const recognition = useRef<Recognition | null>(null)
  // The user wants to listen (from start until stop or cancel), across the browser's own session ends
  const wanted = useRef(false)
  const quickEnds = useRef(0)
  // The field's text as dictation last set it. The next session continues from here: the browser may end a
  // session right after its last result, before the field's state has caught up.
  const dictated = useRef<string | null>(null)
  const latest = useRef({ text, language, onText, onError })
  latest.current = { text, language, onText, onError }

  const session = useCallback(() => {
    const Class = recognitionClass()
    if (!Class) return
    const engine = new Class()
    engine.lang = latest.current.language
    engine.continuous = !SINGLE_UTTERANCE
    engine.interimResults = true
    // Each session continues the field as it is now, so earlier sessions' words are kept
    const before = dictated.current ?? latest.current.text
    const began = Date.now()
    let heard = false
    engine.onresult = (event) => {
      let spoken = ''
      for (let index = 0; index < event.results.length; index++) spoken += event.results[index][0]?.transcript ?? ''
      heard = heard || !!spoken
      dictated.current = joinDictation(before, spoken.trimStart())
      latest.current.onText(dictated.current)
    }
    engine.onerror = (event) => {
      if (FATAL.has(event.error)) {
        wanted.current = false
        latest.current.onError(ERRORS[event.error] ?? `音声入力でエラーが起きました（${event.error}）`)
      } else if (event.error !== 'no-speech' && event.error !== 'aborted') {
        latest.current.onError(`音声入力でエラーが起きました（${event.error}）`)
      }
    }
    engine.onend = () => {
      if (recognition.current !== engine) return
      recognition.current = null
      // Sessions that keep ending at once (the browser refuses) must not loop forever
      quickEnds.current = !heard && Date.now() - began < 1000 ? quickEnds.current + 1 : 0
      if (wanted.current && quickEnds.current < 3) { session(); return }
      wanted.current = false
      setListening(false)
    }
    recognition.current = engine
    try {
      engine.start()
    } catch {
      recognition.current = null
      wanted.current = false
      setListening(false)
      latest.current.onError('音声入力を開始できませんでした')
    }
  }, [])

  const start = useCallback(() => {
    const unavailable = dictationUnavailable()
    if (unavailable) { latest.current.onError(unavailable); return }
    if (wanted.current) return
    wanted.current = true
    quickEnds.current = 0
    dictated.current = null  // start from the field as it is now
    setListening(true)
    session()
  }, [session])

  /** Stop listening; words still being recognised are added. */
  const stop = useCallback(() => {
    wanted.current = false
    if (recognition.current) recognition.current.stop()
    else setListening(false)
  }, [])

  /** Stop at once and drop anything not yet delivered (the field was edited or its text was sent). */
  const cancel = useCallback(() => {
    wanted.current = false
    const engine = recognition.current
    recognition.current = null
    if (engine) { engine.onresult = null; engine.abort() }
    setListening(false)
  }, [])

  /** A different language applies from the next sentence on. */
  const restart = useCallback(() => {
    if (wanted.current) recognition.current?.stop()
  }, [])

  // Leaving the conversation stops listening; the text dictated so far stays in the field
  useEffect(() => () => { wanted.current = false; recognition.current?.abort(); recognition.current = null }, [])

  return { listening, start, stop, cancel, restart, toggle: listening ? stop : start }
}
