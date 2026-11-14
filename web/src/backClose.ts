import { useEffect, useRef } from 'react'

/*
 * While a dialog, drawer or full-screen sheet is open, the browser's back button (or a back swipe on a phone)
 * closes it instead of moving to another conversation underneath. Each open overlay adds one history entry
 * with the same URL; closing it from the screen takes that entry back out.
 */

type Overlay = { id: number; close: () => void }
const stack: Overlay[] = []
let pendingBacks = 0
let next = 0
let listening = false

function current(): number | undefined {
  return (window.history.state as { omniOverlay?: number } | null)?.omniOverlay
}

function onPopState() {
  if (pendingBacks > 0) {
    // The screen closed an overlay and went back itself. If another overlay opened meanwhile, keep its entry on top.
    pendingBacks -= 1
    const top = stack[stack.length - 1]
    if (top && current() !== top.id) window.history.pushState({ ...(window.history.state ?? {}), omniOverlay: top.id }, '')
    return
  }
  const top = stack.pop()
  top?.close()
}

function open(close: () => void): number {
  if (!listening) {
    window.addEventListener('popstate', onPopState)
    listening = true
  }
  const id = ++next
  stack.push({ id, close })
  window.history.pushState({ ...(window.history.state ?? {}), omniOverlay: id }, '')
  return id
}

function closed(id: number) {
  const index = stack.findIndex((item) => item.id === id)
  if (index < 0) return  // already closed by the back button
  stack.splice(index, 1)
  // Leave the URL alone if a conversation was opened from the overlay (the hash added a newer entry)
  if (current() === id) {
    pendingBacks += 1
    window.history.back()
  }
}

export function useBackClose(isOpen: boolean, close: () => void) {
  const latest = useRef(close)
  latest.current = close
  useEffect(() => {
    if (!isOpen) return
    const id = open(() => latest.current())
    return () => closed(id)
  }, [isOpen])
}
