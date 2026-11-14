import { useEffect, useRef, useState } from 'react'
// The legacy build carries the polyfills that phones and tablets a few versions behind still need
import { getDocument, GlobalWorkerOptions, type PDFDocumentProxy } from 'pdfjs-dist/legacy/build/pdf.mjs'
import workerUrl from 'pdfjs-dist/legacy/build/pdf.worker.min.mjs?url'

GlobalWorkerOptions.workerSrc = workerUrl

/** Each URL owns its PDF.js loading task, including cancellation and worker cleanup. */
export function usePdf(url: string | null): { pdf: PDFDocumentProxy | null; error: string | null; loading: boolean } {
  const [result, setResult] = useState<{ url: string; pdf: PDFDocumentProxy | null; error: string | null } | null>(null)
  useEffect(() => {
    if (!url) return
    let active = true
    const task = getDocument({ url, withCredentials: true, useWasm: false })
    task.promise.then((pdf) => { if (active) setResult({ url, pdf, error: null }) })
      .catch((reason) => { if (active) setResult({ url, pdf: null, error: reason instanceof Error ? reason.message : 'PDFを読み込めませんでした' }) })
    return () => { active = false; void task.destroy().catch(() => {}) }
  }, [url])
  const current = result?.url === url ? result : null
  return { pdf: current?.pdf ?? null, error: current?.error ?? null, loading: !!url && !current }
}

/** One page drawn to fit its box: the full width (`fit="width"`), or the whole page inside the box (`fit="page"`). */
export function PdfPage({ pdf, number, fit = 'width', lazy = false, className = '' }: {
  pdf: PDFDocumentProxy; number: number; fit?: 'width' | 'page'; lazy?: boolean; className?: string
}) {
  const box = useRef<HTMLDivElement>(null)
  const canvas = useRef<HTMLCanvasElement>(null)
  const [size, setSize] = useState({ width: 0, height: 0 })
  const [visible, setVisible] = useState(!lazy)
  const [ratio, setRatio] = useState(1.414)

  useEffect(() => {
    const element = box.current!
    const resize = new ResizeObserver(() => setSize({ width: element.clientWidth, height: element.clientHeight }))
    resize.observe(element)
    if (!lazy) return () => resize.disconnect()
    const seen = new IntersectionObserver((entries) => { if (entries.some((entry) => entry.isIntersecting)) setVisible(true) },
      { rootMargin: '600px' })
    seen.observe(element)
    return () => { resize.disconnect(); seen.disconnect() }
  }, [lazy])

  // The box grows with the page it shows, so only a page that must fit a fixed box depends on the box's height
  const height = fit === 'page' ? size.height : 0
  // One canvas takes one render at a time: a newer render waits until the one before it has stopped
  const previous = useRef<Promise<unknown>>(Promise.resolve())
  const [failed, setFailed] = useState<string | null>(null)
  useEffect(() => {
    if (!visible || !size.width || number < 1 || number > pdf.numPages) return
    let cancelled = false
    let task: { cancel: () => void; promise: Promise<unknown> } | null = null
    const before = previous.current
    const done = (async () => {
      await before.catch(() => {})
      if (cancelled) return
      const page = await pdf.getPage(number)
      const target = canvas.current
      if (cancelled || !target) return
      const base = page.getViewport({ scale: 1 })
      setRatio(base.height / base.width)
      const scale = height ? Math.min(size.width / base.width, height / base.height) : size.width / base.width
      const density = Math.min(window.devicePixelRatio || 1, 2)
      const viewport = page.getViewport({ scale: scale * density })
      target.width = Math.floor(viewport.width)
      target.height = Math.floor(viewport.height)
      target.style.width = `${Math.floor(viewport.width / density)}px`
      target.style.height = `${Math.floor(viewport.height / density)}px`
      task = page.render({ canvas: target, viewport })
      await task.promise
      setFailed(null)
    })()
    previous.current = done
    done.catch((reason) => {
      if (!cancelled) setFailed(reason instanceof Error ? reason.message : 'ページを描画できませんでした')
    })
    return () => { cancelled = true; task?.cancel() }
  }, [pdf, number, visible, size.width, height])

  return <div ref={box} className={`${fit === 'page' ? 'flex h-full w-full items-center justify-center' : 'w-full'} ${className}`}
    style={fit === 'width' && size.width ? { minHeight: size.width * ratio } : undefined}>
    {failed && <p role="alert" className="p-3 text-sm text-danger">{number}ページを表示できません: {failed}</p>}
    <canvas ref={canvas} role="img" aria-label={`${number}ページ`} className={`bg-white shadow ${failed ? 'hidden' : ''}`} />
  </div>
}

/** Every page of a PDF, top to bottom. Pages are drawn when they come near the screen. */
export default function PdfView({ url }: { url: string | null }) {
  const { pdf, error, loading } = usePdf(url)
  if (error && !pdf) return <p role="alert" className="p-4 text-sm text-danger">PDFを表示できません: {error}</p>
  if (!pdf) return <p className="p-4 text-sm text-muted">{loading ? 'PDFを読み込み中…' : ''}</p>
  return <div className="mx-auto flex w-full max-w-4xl flex-col gap-3 p-3">
    {Array.from({ length: pdf.numPages }, (_, index) => <PdfPage key={index} pdf={pdf} number={index + 1} lazy />)}
  </div>
}
