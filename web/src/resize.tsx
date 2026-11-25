/** Panes the user can widen or narrow by dragging their edge (wide screens only), remembered in this browser. */
import { useCallback, useEffect, useRef, useState, type CSSProperties } from 'react'

const KEY = 'omni.width.'
const STEP = 16
// What the content beside a pane always keeps, so a pane can never squeeze it away
const KEEP = 320

type Limits = { initial: number; min: number; max: number }

function stored(name: string): number | null {
  try {
    const value = Number(localStorage.getItem(KEY + name))
    return Number.isFinite(value) && value > 0 ? value : null
  } catch {
    return null
  }
}

/** The largest width that still leaves room for the rest of the screen. */
function ceiling({ min, max }: Limits, others: number): number {
  return Math.max(min, Math.min(max, window.innerWidth - others - KEEP))
}

export function clampWidth(value: number, limits: Limits, others = 0): number {
  return Math.round(Math.min(Math.max(value, limits.min), ceiling(limits, others)))
}

/**
 * A remembered width. `others` is the width taken by other panes on the same row (so the main content keeps
 * at least KEEP pixels). The value is re-clamped when the window gets smaller.
 */
export function useWidth(name: string, limits: Limits, others = 0) {
  const [width, setWidth] = useState(() => stored(name) ?? limits.initial)
  const [, setViewport] = useState(0)
  useEffect(() => {
    const resized = () => setViewport(window.innerWidth)
    window.addEventListener('resize', resized)
    return () => window.removeEventListener('resize', resized)
  }, [])
  const change = useCallback((value: number) => {
    setWidth(value)
    try { localStorage.setItem(KEY + name, String(Math.round(value))) } catch { /* storage disabled */ }
  }, [name])
  const reset = useCallback(() => {
    setWidth(limits.initial)
    try { localStorage.removeItem(KEY + name) } catch { /* storage disabled */ }
  }, [name, limits.initial])
  return { width: clampWidth(width, limits, others), setWidth: change, reset }
}

/** The CSS variable a pane reads its width from, e.g. `md:w-[var(--pane-w)]`. */
export const widthVar = (name: string, width: number) => ({ [`--${name}-w`]: `${width}px` }) as CSSProperties

/**
 * The draggable edge of a pane. `edge` is where it sits on the pane: "right" grows the pane to the right
 * (a left sidebar), "left" grows it to the left (a right panel). Also works with the arrow keys, and a
 * double click restores the default width.
 */
export function Resizer({ label, edge, width, limits, others = 0, onChange, onReset, className = '' }: {
  label: string; edge: 'left' | 'right'; width: number; limits: Limits; others?: number
  onChange: (width: number) => void; onReset: () => void; className?: string
}) {
  const drag = useRef<{ x: number; width: number } | null>(null)
  const [active, setActive] = useState(false)
  const direction = edge === 'right' ? 1 : -1
  const set = (value: number) => onChange(clampWidth(value, limits, others))

  useEffect(() => {
    if (!active) return
    // While dragging, keep the cursor and stop selecting text across the page
    const { cursor, userSelect } = document.body.style
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    return () => { document.body.style.cursor = cursor; document.body.style.userSelect = userSelect }
  }, [active])

  return <div role="separator" aria-orientation="vertical" aria-label={label} tabIndex={0}
    aria-valuenow={width} aria-valuemin={limits.min} aria-valuemax={ceiling(limits, others)}
    title={`${label}（ドラッグで幅を変更、ダブルクリックで元に戻す）`}
    onPointerDown={(event) => {
      if (event.button !== 0) return
      event.preventDefault()
      event.currentTarget.setPointerCapture(event.pointerId)
      drag.current = { x: event.clientX, width }
      setActive(true)
    }}
    onPointerMove={(event) => {
      if (drag.current) set(drag.current.width + (event.clientX - drag.current.x) * direction)
    }}
    onPointerUp={() => { drag.current = null; setActive(false) }}
    onPointerCancel={() => { drag.current = null; setActive(false) }}
    onDoubleClick={onReset}
    onKeyDown={(event) => {
      const step = event.shiftKey ? STEP * 4 : STEP
      if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
        event.preventDefault()
        set(width + (event.key === 'ArrowRight' ? step : -step) * direction)
      } else if (event.key === 'Home' || event.key === 'End') {
        event.preventDefault()
        set(event.key === 'Home' ? limits.min : limits.max)
      } else if (event.key === 'Enter') {
        event.preventDefault()
        onReset()
      }
    }}
    className={`group relative z-10 -mx-1.5 w-3 shrink-0 cursor-col-resize touch-none outline-none ${className}`}>
    {/* A 12px grip (overlapping its neighbours, so the layout is unchanged) around a thin line that
        lights up on hover, focus and drag */}
    <span aria-hidden className={`absolute inset-y-0 left-1/2 w-0.5 -translate-x-1/2 transition-colors ${active ? 'bg-accent' : 'bg-transparent group-hover:bg-accent/60 group-focus-visible:bg-accent'}`} />
  </div>
}

// Default, narrowest and widest widths of each pane, in pixels
export const SIDEBAR = { initial: 288, min: 200, max: 560 }
export const PANEL = { initial: Math.min(576, Math.round(window.innerWidth * 0.4)), min: 300, max: 1400 }
export const TREE = { initial: 256, min: 180, max: 600 }
export const PREVIEW = { initial: Math.max(360, Math.round(window.innerWidth * 0.35)), min: 280, max: 1600 }
