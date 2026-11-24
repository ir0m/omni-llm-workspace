/**
 * Copy text to the clipboard. The Clipboard API exists only on secure pages (HTTPS or localhost); opened over
 * plain HTTP (e.g. a LAN or VPN address) it is missing, so fall back to the older copy command.
 */
export async function copyText(text: string): Promise<boolean> {
  if (window.isSecureContext && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // Denied (e.g. the document lost focus): try the fallback below
    }
  }
  const area = document.createElement('textarea')
  area.value = text
  area.setAttribute('readonly', '')
  // Off screen but selectable; a fixed position keeps the page from scrolling to it
  area.style.cssText = 'position:fixed;top:0;left:-9999px;opacity:0'
  document.body.appendChild(area)
  const focused = document.activeElement as HTMLElement | null
  try {
    area.select()
    area.setSelectionRange(0, text.length)
    return document.execCommand('copy')
  } catch {
    return false
  } finally {
    area.remove()
    focused?.focus?.({ preventScroll: true })
  }
}

type HastNode = { type: string; value?: string; children?: HastNode[] }

/** All the text under a syntax tree node, including the text inside highlighting spans. */
export function nodeText(node: HastNode | undefined): string {
  if (!node) return ''
  if (node.type === 'text') return node.value ?? ''
  return (node.children ?? []).map(nodeText).join('')
}
