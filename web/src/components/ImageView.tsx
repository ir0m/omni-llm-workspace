import { useEffect, useState } from 'react'
import { ExternalLink, Maximize2, Minimize2 } from 'lucide-react'

/**
 * An image shown to fit the pane, or at its actual size (click or the button). A checkerboard shows through
 * transparent parts. `vector` (SVG) fills the pane when fitted, since SVGs often have no fixed pixel size.
 */
export default function ImageView({ src, name, vector = false, openable = true }: {
  src: string; name: string; vector?: boolean; openable?: boolean
}) {
  const [actual, setActual] = useState(false)
  const [size, setSize] = useState<{ width: number; height: number } | null>(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => { setFailed(false); setSize(null) }, [src])

  const fitted = vector ? 'h-full w-full object-contain' : 'max-h-full max-w-full object-contain'
  return <div className="flex min-h-0 min-w-0 flex-1 flex-col">
    <div className="flex items-center gap-2 border-b border-border px-3 py-1 text-xs text-muted">
      <span className="min-w-0 flex-1 truncate" role="status">
        {failed ? '画像を表示できませんでした（形式や内容を確認してください）' : size ? `${size.width} × ${size.height} px` : '読み込み中…'}
      </span>
      <button onClick={() => setActual((value) => !value)} disabled={failed} aria-pressed={actual}
        className="flex items-center gap-1 rounded px-1.5 py-0.5 hover:text-fg disabled:opacity-40">
        {actual ? <Minimize2 className="size-3.5" aria-hidden /> : <Maximize2 className="size-3.5" aria-hidden />}
        {actual ? '全体を表示' : '原寸で表示'}
      </button>
      {openable && <a href={src} target="_blank" rel="noopener noreferrer" className="flex items-center gap-1 rounded px-1.5 py-0.5 hover:text-fg">
        <ExternalLink className="size-3.5" aria-hidden />別のタブで開く
      </a>}
    </div>
    <div className={`checkerboard min-h-0 flex-1 overflow-auto ${actual ? '' : 'flex items-center justify-center p-3'}`}>
      {!failed && <img src={src} alt={name} draggable={false}
        onLoad={(event) => setSize({ width: event.currentTarget.naturalWidth, height: event.currentTarget.naturalHeight })}
        onError={() => setFailed(true)}
        onClick={() => setActual((value) => !value)}
        className={actual ? 'max-w-none cursor-zoom-out' : `${fitted} cursor-zoom-in`} />}
    </div>
  </div>
}
