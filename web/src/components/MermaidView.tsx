import { useEffect, useId, useState } from 'react'
let queue = Promise.resolve()

/** Render in a frame with no script, network or parent-page access. */
export default function MermaidView({ text }: { text: string }) {
  const id = useId().replace(/[^a-z0-9]/gi, '')
  const [document, setDocument] = useState('')
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true
    queue = queue.catch(() => {}).then(async () => {
      if (!active) return
      try {
        const { default: mermaid } = await import('mermaid')
        mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', suppressErrorRendering: true, maxTextSize: 50000 })
        const { svg } = await mermaid.render('mermaid' + id, text)
        if (active) {
          setDocument(`<html><head><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:"><style>body{margin:0}svg{width:100%;max-height:420px}</style></head><body>${svg}</body></html>`)
          setError('')
        }
      } catch (reason) { if (active) setError(reason instanceof Error ? reason.message : '図を表示できません') }
    })
    return () => { active = false }
  }, [text, id])
  if (error) return <pre className="text-danger">{error}</pre>
  return document ? <iframe sandbox="" title="Mermaidの図" srcDoc={document} className="my-3 h-[440px] w-full border-0 bg-white" /> : <span>図を描画中…</span>
}
