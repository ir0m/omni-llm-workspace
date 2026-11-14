import { useEffect, useRef } from 'react'
import { api } from './api'

export type MarpDocument = { slides: string[]; css: string; notes: string[] }
export async function renderMarp(text: string, project: string, path: string): Promise<MarpDocument> {
  const { Marp } = await import('@marp-team/marp-core')
  const { html, css, comments } = new Marp({ html: false, script: false, math: 'katex', inlineSVG: true, container: false }).render(text, { htmlAsArray: true })
  const imageUrl = (src: string): string => {
    if (/^[a-z][a-z0-9+.-]*:/i.test(src) || src.startsWith('//')) return src.startsWith('data:image/') ? src : ''
    const parts = src.startsWith('/') ? [] : path.split('/').slice(0, -1)
    let decoded = src
    try { decoded = decodeURIComponent(src) } catch { return '' }
    for (const part of decoded.split('/')) {
      if (!part || part === '.') continue
      if (part === '..') { if (!parts.length) return ''; parts.pop() } else parts.push(part)
    }
    return api.imageUrl(project, parts.join('/'), 0)
  }
  const slides = html.map((value) => {
    const doc = new DOMParser().parseFromString(value, 'text/html')
    doc.querySelectorAll('img').forEach((img) => img.setAttribute('src', imageUrl(img.getAttribute('src') ?? '')))
    doc.querySelectorAll('[style]').forEach((element) => {
      element.setAttribute('style', (element.getAttribute('style') ?? '').replace(/url\(["']?([^"')]+)["']?\)/g, (_, url: string) => `url("${imageUrl(url)}")`))
    })
    return doc.body.innerHTML
  })
  return { slides, css, notes: comments.map((lines) => lines.join('\n\n')) }
}

/** Theme CSS is scoped to this slide, so it cannot change the editor or other slides. */
export function MarpFrame({ marp, index }: { marp: MarpDocument; index: number }) {
  const host = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const root = host.current!.shadowRoot ?? host.current!.attachShadow({ mode: 'open' })
    const style = document.createElement('style')
    style.textContent = `${marp.css}\n:host{display:block;width:100%;height:100%}.marpit{width:100%;height:100%}.marpit>svg{display:block;width:100%;height:100%}`
    const slide = document.createElement('div')
    slide.className = 'marpit'
    slide.innerHTML = marp.slides[index] ?? ''
    root.replaceChildren(style, slide)
  }, [marp, index])
  return <div ref={host} className="h-full w-full" />
}
