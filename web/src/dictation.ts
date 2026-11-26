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

/** Joins dictated text to what is already typed, with a space where both sides are words of a spaced language. */
export function joinDictation(before: string, spoken: string): string {
  if (!before || !spoken) return before + spoken
  const needsSpace = /[A-Za-z0-9.,!?;:]$/.test(before) && /^[A-Za-z0-9]/.test(spoken)
  return before + (needsSpace ? ' ' : '') + spoken
}

/**
 * Dictation into a text field. While listening, `onText` receives the field's text: what was there when
 * dictation started, then what was recognised so far (including words still being recognised).
 */
export function useDictation({ text, onText, onError }: {
  text: string; onText: (value: string) => void; onError: (message: string) => void
}) {
  const [listening, setListening] = useState(false)
  const recognition = useRef<Recognition | null>(null)
  const latest = useRef({ text, onText, onError })
  latest.current = { text, onText, onError }

  /** Stop listening; words still being recognised are added. */
  const stop = useCallback(() => {
    recognition.current?.stop()
  }, [])

  /** Stop at once and drop anything not yet delivered (the field was edited or its text was sent). */
  const cancel = useCallback(() => {
    const engine = recognition.current
    if (!engine) return
    recognition.current = null
    engine.onresult = null
    engine.abort()
    setListening(false)
  }, [])

  const start = useCallback(() => {
    const Class = recognitionClass()
    const unavailable = dictationUnavailable()
    if (!Class || unavailable) { latest.current.onError(unavailable ?? '音声入力を使えません'); return }
    if (recognition.current) return
    const engine = new Class()
    engine.lang = navigator.language || 'ja-JP'
    engine.continuous = true
    engine.interimResults = true
    const before = latest.current.text
    engine.onresult = (event) => {
      let spoken = ''
      for (let index = 0; index < event.results.length; index++) spoken += event.results[index][0]?.transcript ?? ''
      latest.current.onText(joinDictation(before, spoken.trimStart()))
    }
    engine.onerror = (event) => {
      // Silence and a stop requested by the user are not failures
      if (event.error !== 'no-speech' && event.error !== 'aborted') latest.current.onError(ERRORS[event.error] ?? `音声入力でエラーが起きました（${event.error}）`)
    }
    engine.onend = () => {
      if (recognition.current === engine) recognition.current = null
      setListening(false)
    }
    recognition.current = engine
    try {
      engine.start()
      setListening(true)
    } catch {
      recognition.current = null
      latest.current.onError('音声入力を開始できませんでした')
    }
  }, [])

  // Leaving the conversation stops listening; the text dictated so far stays in the field
  useEffect(() => () => { recognition.current?.abort(); recognition.current = null }, [])

  return { listening, start, stop, cancel, toggle: listening ? stop : start }
}
