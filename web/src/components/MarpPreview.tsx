import { useEffect, useState } from 'react'
import { MarpFrame, renderMarp, type MarpDocument } from '../marp'

export default function MarpPreview({ text, project, path }: { text: string; project: string; path: string }) {
  const [rendered, setRendered] = useState<MarpDocument | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let active = true
    renderMarp(text, project, path).then((value) => { if (active) { setRendered(value); setError(null) } })
      .catch((reason) => { if (active) setError(reason instanceof Error ? reason.message : 'スライドを表示できませんでした') })
    return () => { active = false }
  }, [text, project, path])
  if (error) return <p role="alert" className="p-4 text-sm text-danger">{error}</p>
  return <div className="mx-auto flex w-full max-w-4xl flex-col gap-3">
    {rendered?.slides.map((_, index) => <div key={index} aria-label={`スライド ${index + 1}`} className="aspect-video w-full overflow-hidden shadow">
      <MarpFrame marp={rendered} index={index} />
    </div>)}
  </div>
}
