import { useEffect, useState, type ReactNode } from 'react'
import type { PDFDocumentProxy } from 'pdfjs-dist'
import { api } from './api'
import { renderMarp, MarpFrame, type MarpDocument } from './marp'
import { PdfPage, usePdf } from './components/PdfView'

import type { DeckSpec } from './deckSpec'
export type { DeckSpec } from './deckSpec'
export type Deck = { count: number; notes: string[]; slide: (index: number) => ReactNode }

/** Speaker notes for a PDF live beside it as `<name>.notes.md`: one section per page, separated by `---` lines. */
export const notesPath = (path: string) => path.replace(/\.[^./]+$/, '') + '.notes.md'
const splitNotes = (text: string) => text.split(/^---[ \t]*$/m).map((part) => part.trim())

export function useDeck(spec: DeckSpec | null): { deck: Deck | null; error: string | null } {
  const pdfUrl = spec?.kind === 'pdf' ? api.pdfUrl(spec.project, spec.path, spec.version) : null
  const { pdf, error: pdfError } = usePdf(pdfUrl)
  const [marp, setMarp] = useState<MarpDocument | null>(null)
  const [notes, setNotes] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)
  const key = spec ? `${spec.project}\n${spec.path}\n${spec.kind}\n${spec.version}` : null

  useEffect(() => {
    if (!spec) { setMarp(null); setNotes([]); return }
    let cancelled = false
    setError(null)
    setMarp(null); setNotes([])
    if (spec.kind === 'marp') {
      api.fileFull(spec.project, spec.path).then(async (file) => {
        if (file.kind !== 'text') throw new Error('スライドのMarkdownを読み込めませんでした')
        const rendered = await renderMarp(file.text, spec.project, spec.path)
        if (!cancelled) setMarp(rendered)
      }).catch((reason) => { if (!cancelled) setError(reason instanceof Error ? reason.message : 'スライドを表示できませんでした') })
    } else {
      setMarp(null)
      api.fileFull(spec.project, notesPath(spec.path)).then((file) => {
        if (!cancelled) setNotes(file.kind === 'text' ? splitNotes(file.text) : [])
      }).catch(() => { if (!cancelled) setNotes([]) })  // no notes file: no notes
    }
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  if (!spec) return { deck: null, error: null }
  if (spec.kind === 'marp') {
    return { error, deck: marp && { count: marp.slides.length, notes: marp.notes, slide: (index) => <MarpFrame marp={marp} index={index} /> } }
  }
  return { error: pdfError, deck: pdf && { count: pdf.numPages, notes, slide: (index) => <PdfSlide pdf={pdf} index={index} /> } }
}

function PdfSlide({ pdf, index }: { pdf: PDFDocumentProxy; index: number }) {
  return <PdfPage pdf={pdf} number={index + 1} fit="page" />
}
