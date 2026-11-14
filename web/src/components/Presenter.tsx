import { useCallback, useEffect, useRef, useState } from 'react'
import { Printer, ChevronLeft, ChevronRight, Maximize, MonitorUp, Pause, Play, RotateCcw, X } from 'lucide-react'
import { useBackClose } from '../backClose'
import { useDeck, type DeckSpec } from '../slides'
import { Markdown } from './MessageView'
import { audienceHash } from './presentLink'

const CHANNEL = 'omni-present'
type Message = { type: 'state'; deck: string; page: number; blank: boolean } | { type: 'hello'; deck: string } | { type: 'closed'; deck: string }
const deckKey = (spec: DeckSpec) => `${spec.project}\n${spec.path}\n${spec.kind}`

function clock(seconds: number): string {
  const part = (value: number) => String(value).padStart(2, '0')
  return `${part(Math.floor(seconds / 3600))}:${part(Math.floor(seconds / 60) % 60)}:${part(seconds % 60)}`
}

/** The second window, for the projector: only the slide, following the presenter's window. */
export function Audience({ spec }: { spec: DeckSpec }) {
  const { deck, error } = useDeck(spec)
  const [page, setPage] = useState(0)
  const [blank, setBlank] = useState(false)
  const [connected, setConnected] = useState(false)
  useEffect(() => {
    const channel = new BroadcastChannel(CHANNEL)
    channel.onmessage = (event: MessageEvent<Message>) => {
      if (event.data.deck !== deckKey(spec)) return
      if (event.data.type === 'closed') { setConnected(false); return }
      if (event.data.type !== 'state') return
      setPage(event.data.page)
      setBlank(event.data.blank)
      setConnected(true)
    }
    channel.postMessage({ type: 'hello', deck: deckKey(spec) } satisfies Message)
    return () => channel.close()
  }, [spec])
  useEffect(() => { document.title = `発表: ${spec.path}` }, [spec.path])
  // Without the presenter's window (it was closed, or this one was opened alone) the keys still turn pages
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'f' || event.key === 'F') void document.documentElement.requestFullscreen?.().catch(() => {})
      if (connected || !deck) return
      if (['ArrowRight', 'PageDown', ' '].includes(event.key)) setPage((value) => Math.min(value + 1, deck.count - 1))
      if (['ArrowLeft', 'PageUp'].includes(event.key)) setPage((value) => Math.max(value - 1, 0))
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [connected, deck])

  const printing = new URLSearchParams(window.location.hash.split('?')[1]).get('print') === '1'
  if (printing) return <main className="print-deck bg-white">
    <div className="print-controls p-3"><button className="rounded border px-3 py-2" disabled={!deck} onClick={() => window.print()}>印刷ダイアログで「PDFに保存」を選択</button></div>
    {deck && Array.from({ length: deck.count }, (_, index) => <div key={index} className="print-slide aspect-video w-full">{deck.slide(index)}</div>)}
    {error && <p role="alert">{error}</p>}
  </main>

  return <main className="group fixed inset-0 bg-black" onDoubleClick={() => void document.documentElement.requestFullscreen?.().catch(() => {})}>
    {error && <p role="alert" className="p-6 text-white">{error}</p>}
    {deck && !blank && deck.slide(Math.min(page, deck.count - 1))}
    <button onClick={() => void document.documentElement.requestFullscreen?.().catch(() => {})}
      className="absolute bottom-3 right-3 flex items-center gap-1 rounded-lg bg-white/15 px-3 py-1.5 text-sm text-white opacity-0 transition-opacity focus:opacity-100 group-hover:opacity-100">
      <Maximize className="size-4" aria-hidden />全画面（F）
    </button>
  </main>
}

/** The presenter's own screen: the current and next slide, the notes and a timer. Drives the audience window. */
export default function Presenter({ spec, onClose }: { spec: DeckSpec; onClose: () => void }) {
  const { deck, error } = useDeck(spec)
  const [page, setPage] = useState(0)
  const [blank, setBlank] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [running, setRunning] = useState(false)
  const [blocked, setBlocked] = useState(false)
  const channel = useRef<BroadcastChannel | null>(null)
  const state = useRef({ page, blank })
  state.current = { page, blank }
  const root = useRef<HTMLDivElement>(null)
  useBackClose(true, onClose)

  const count = deck?.count ?? 0
  const go = useCallback((next: number) => {
    setPage((value) => Math.max(0, Math.min(typeof next === 'number' ? next : value, Math.max(count - 1, 0))))
    setRunning(true)  // the talk has started once a page is turned
  }, [count])

  useEffect(() => {
    const opened = new BroadcastChannel(CHANNEL)
    channel.current = opened
    opened.onmessage = (event: MessageEvent<Message>) => {
      if (event.data.type === 'hello' && event.data.deck === deckKey(spec)) {
        opened.postMessage({ type: 'state', deck: deckKey(spec), ...state.current } satisfies Message)
      }
    }
    return () => { opened.postMessage({ type: 'closed', deck: deckKey(spec) } satisfies Message); opened.close(); channel.current = null }
  }, [spec])
  useEffect(() => {
    channel.current?.postMessage({ type: 'state', deck: deckKey(spec), page, blank } satisfies Message)
  }, [spec, page, blank])

  useEffect(() => {
    if (!running) return
    const timer = window.setInterval(() => setElapsed((value) => value + 1), 1000)
    return () => window.clearInterval(timer)
  }, [running])

  useEffect(() => { root.current?.focus() }, [])

  function onKey(event: React.KeyboardEvent) {
    if (event.target instanceof HTMLElement && event.target.closest('button, a, input, select, textarea') && [' ', 'Enter'].includes(event.key)) return
    if (['ArrowRight', 'ArrowDown', 'PageDown', ' ', 'Enter'].includes(event.key)) go(page + 1)
    else if (['ArrowLeft', 'ArrowUp', 'PageUp', 'Backspace'].includes(event.key)) go(page - 1)
    else if (event.key === 'Home') go(0)
    else if (event.key === 'End') go(count - 1)
    else if (event.key === 'b' || event.key === 'B' || event.key === '.') setBlank((value) => !value)
    else if (event.key === 'Escape') onClose()
    else return
    event.preventDefault()
    event.stopPropagation()
  }

  function openAudience() {
    const opened = window.open(`${window.location.pathname}${audienceHash(spec)}`, 'omni-audience', 'popup,width=1280,height=720')
    setBlocked(!opened)
  }

  const button = 'flex items-center gap-1 rounded-lg border border-white/25 px-3 py-1.5 text-sm hover:bg-white/10 disabled:opacity-40'
  const note = deck?.notes[page]?.trim()
  return <div ref={root} tabIndex={-1} role="dialog" aria-modal aria-label="発表者ビュー" onKeyDown={onKey}
    className="fixed inset-0 z-[60] flex flex-col bg-neutral-900 text-white outline-none">
    <header className="flex flex-wrap items-center gap-2 border-b border-white/15 px-3 py-2">
      <span className="min-w-0 flex-1 truncate text-sm text-white/70">{spec.path}</span>
      <span className="font-mono text-lg tabular-nums" aria-label="経過時間">{clock(elapsed)}</span>
      <button onClick={() => setRunning((value) => !value)} className={button} aria-label={running ? 'タイマーを止める' : 'タイマーを動かす'}>
        {running ? <Pause className="size-4" aria-hidden /> : <Play className="size-4" aria-hidden />}
      </button>
      <button onClick={() => { setElapsed(0); setRunning(false) }} className={button} aria-label="タイマーを戻す"><RotateCcw className="size-4" aria-hidden /></button>
      {spec.kind === 'marp' && <button onClick={() => {
        setBlocked(!window.open(`${window.location.pathname}${audienceHash(spec)}&print=1`, '_blank'))
      }} className={button}><Printer className="size-4" aria-hidden />PDFに保存・印刷</button>}
      <button onClick={openAudience} className={button}><MonitorUp className="size-4" aria-hidden />投影用の画面を開く</button>
      <button onClick={onClose} className={button} aria-label="発表者ビューを閉じる"><X className="size-4" aria-hidden /></button>
    </header>
    {blocked && <p role="alert" className="bg-amber-500/20 px-3 py-1.5 text-sm">ブラウザが新しい画面を止めました。このサイトのポップアップを許可してください。</p>}
    {error && <p role="alert" className="p-4 text-red-300">{error}</p>}
    {!deck && !error && <p className="p-4 text-white/60">スライドを読み込み中…</p>}
    {deck && <div className="flex min-h-0 flex-1 flex-col gap-3 p-3 lg:flex-row">
      <div className="relative min-h-0 flex-[3] bg-black">
        {deck.slide(page)}
        {blank && <div className="absolute inset-0 flex items-center justify-center bg-black/80 text-sm text-white/70">投影は暗転中（B で戻す）</div>}
      </div>
      <div className="flex min-h-0 flex-[2] flex-col gap-3">
        <div className="relative h-40 shrink-0 bg-black lg:h-auto lg:flex-1" aria-label="次のスライド">
          {page + 1 < count ? deck.slide(page + 1) : <p className="flex h-full items-center justify-center text-sm text-white/50">最後のスライドです</p>}
        </div>
        <section aria-label="発表者ノート" className="min-h-0 flex-1 overflow-y-auto rounded-lg bg-white/5 p-3 text-base leading-relaxed">
          {note ? <Markdown text={note} /> : <p className="text-sm text-white/40">このスライドのノートはありません</p>}
        </section>
      </div>
    </div>}
    <footer className="flex items-center justify-center gap-3 border-t border-white/15 px-3 py-2 pb-[max(0.5rem,env(safe-area-inset-bottom))]">
      <button onClick={() => go(page - 1)} disabled={page <= 0} className={button} aria-label="前のスライド"><ChevronLeft className="size-5" aria-hidden /></button>
      <span className="min-w-20 text-center font-mono tabular-nums" aria-live="polite">{count ? page + 1 : 0} / {count}</span>
      <button onClick={() => go(page + 1)} disabled={page >= count - 1} className={button} aria-label="次のスライド"><ChevronRight className="size-5" aria-hidden /></button>
      <button onClick={() => setBlank((value) => !value)} aria-pressed={blank} className={button}>暗転（B）</button>
    </footer>
  </div>
}
