import { lazy, Suspense, useEffect, useMemo, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkFrontmatter from 'remark-frontmatter'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import { visit } from 'unist-util-visit'
import type { Root, Text, PhrasingContent } from 'mdast'
import { api, type NoteIndex } from '../api'
import { resolveNote, noteSection } from '../noteLinks'
import 'katex/dist/katex.min.css'

const MermaidView = lazy(() => import('./MermaidView'))
const PdfView = lazy(() => import('./PdfView'))
const WIKI = '/__omni/wiki/'
const TAG = '/__omni/tag/'

/** Transform text nodes only: code, HTML and link destinations are never interpreted as wikilinks. */
function obsidian() {
  return (tree: Root) => {
    visit(tree, 'text', (node: Text, index, parent) => {
      if (index === undefined || !parent || parent.type === 'link') return
      const pattern = /!?\[\[([^\]\n]+)\]\]|(^|\s)#([\p{L}_][\p{L}\p{N}_/-]*)/gu
      const nodes: PhrasingContent[] = []
      let end = 0
      for (const match of node.value.matchAll(pattern)) {
        const start = match.index!
        if (start > end) nodes.push({ type: 'text', value: node.value.slice(end, start) })
        if (match[1]) {
          const [target, ...label] = match[1].split('|')
          const url = WIKI + encodeURIComponent(target)
          nodes.push(match[0].startsWith('!') ? { type: 'image', url, alt: label.join('|') || target }
            : { type: 'link', url, children: [{ type: 'text', value: label.join('|') || target }] })
        } else {
          nodes.push({ type: 'text', value: match[2] }, { type: 'link', url: TAG + encodeURIComponent(match[3]), children: [{ type: 'text', value: '#' + match[3] }] })
        }
        end = start + match[0].length
      }
      if (!nodes.length) return
      if (end < node.value.length) nodes.push({ type: 'text', value: node.value.slice(end) })
      parent.children.splice(index, 1, ...nodes as typeof parent.children)
      return index + nodes.length
    })
    visit(tree, 'paragraph', (node) => {
      if (node.children.some((child) => child.type === 'image' && child.url.startsWith(WIKI))) node.data = { hName: 'div', hProperties: { className: ['note-embeds'] } }
    })
    visit(tree, 'blockquote', (node) => {
      const paragraph = node.children[0]
      if (paragraph?.type !== 'paragraph' || paragraph.children[0]?.type !== 'text') return
      const first = paragraph.children[0]
      const match = first.value.match(/^\[!([\w-]+)\]([+-])?\s*([^\n]*)(?:\n|$)/)
      if (!match) return
      first.value = first.value.slice(match[0].length)
      const title = match[3] || match[1]
      node.data = { hName: match[2] ? 'details' : 'aside', hProperties: { className: ['note-callout'], ...(match[2] === '+' ? { open: true } : {}) } }
      node.children.unshift({ type: 'paragraph', data: { hName: match[2] ? 'summary' : 'strong' }, children: [{ type: 'text', value: title }] })
    })
  }
}

type Props = { text: string; project: string; path: string; index: NoteIndex; version: number
  onOpen: (path: string, fragment?: string) => void; onSearch: (query: string) => void; trail?: string[] }

function Embed({ target, label, context }: { target: string; label: string; context: Props }) {
  const path = resolveNote(context.path, target, context.index)
  const trail = context.trail ?? [context.path]
  const fragment = target.split('#').slice(1).join('#')
  const [text, setText] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const markdown = !!path && /\.(md|markdown)$/i.test(path)
  const recursive = !!path && (trail.includes(path) || trail.length >= 4)
  useEffect(() => {
    let active = true
    setText(null); setError(null)
    if (markdown && !recursive) api.fileFull(context.project, path!).then((file) => {
      if (!active) return
      if (file.kind !== 'text' || file.truncated) throw new Error('埋め込みを読み込めません')
      setText(file.text)
    }).catch((reason) => { if (active) setError(String(reason)) })
    return () => { active = false }
  }, [context.project, path, context.version, markdown, recursive])
  if (!path) return <span className="text-muted">［リンク先がないか同名です: {target}］</span>
  if (/\.(png|jpe?g|gif|webp)$/i.test(path)) {
    const width = /^\d+(x\d+)?$/.test(label) ? Math.min(Number(label.split('x')[0]), 4096) : undefined
    return <img src={api.imageUrl(context.project, path, context.version)} alt={target} width={width} loading="lazy" />
  }
  return <section className="my-3 rounded-lg border border-border p-3" aria-label={`埋め込み: ${path}`}>
    <button className="text-xs text-accent" onClick={() => context.onOpen(path, fragment)}>{path}{fragment && '#' + fragment}</button>
    {recursive ? <p className="text-xs text-muted">循環参照、または埋め込みの深さの上限です</p>
      : error ? <p className="text-xs text-danger">{error}</p>
      : markdown ? text !== null ? <NoteMarkdown {...context} path={path} text={noteSection(text, fragment)} trail={[...trail, path]} /> : <p>読み込み中…</p>
      : /\.pdf$/i.test(path) ? <div className="max-h-96 overflow-auto"><Suspense fallback={null}><PdfView url={api.pdfUrl(context.project, path, context.version)} /></Suspense></div> : <p className="text-xs text-muted">上のリンクから開けます</p>}
  </section>
}

export default function NoteMarkdown(props: Props) {
  const frontmatter = useMemo(() => props.text.match(/^---\r?\n([\s\S]*?)\r?\n(?:---|\.\.\.)(?:\r?\n|$)/)?.[1], [props.text])
  return <div className="markdown">
    {frontmatter && <details className="mb-3 rounded-lg border border-border p-2 text-xs"><summary>プロパティ</summary><pre>{frontmatter}</pre></details>}
    <ReactMarkdown remarkPlugins={[remarkGfm, remarkFrontmatter, remarkMath, obsidian]} rehypePlugins={[[rehypeKatex, { strict: false, trust: false }]]}
      components={{
        pre: ({ node, children }) => {
          const code = node?.children[0]
          if (code?.type === 'element' && Array.isArray(code.properties.className) && code.properties.className.includes('language-mermaid')) {
            const text = code.children.map((child) => child.type === 'text' ? child.value : '').join('')
            return <Suspense fallback={<p>図を読み込み中…</p>}><MermaidView text={text} /></Suspense>
          }
          return <pre>{children}</pre>
        },
        a: ({ href = '', children }) => {
          if (href.startsWith(TAG)) return <button className="text-accent" onClick={() => props.onSearch(decodeURIComponent(href.slice(TAG.length)))}>{children}</button>
          const wiki = href.startsWith(WIKI)
          const target = wiki ? decodeURIComponent(href.slice(WIKI.length)) : href
          if (!wiki && /^(https?:|mailto:)/i.test(target)) return <a href={target} target="_blank" rel="noopener noreferrer">{children}</a>
          const path = resolveNote(props.path, target, props.index, wiki)
          return <button className={path ? 'text-accent underline' : 'text-muted underline decoration-dashed'} title={path ?? 'リンク先がないか、同名のファイルが複数あります'}
            disabled={!path} onClick={() => path && props.onOpen(path, decodeURIComponent(target.split('#').slice(1).join('#')))}>{children}</button>
        },
        img: ({ src = '', alt = '' }) => {
          if (src.startsWith(WIKI)) return <Embed target={decodeURIComponent(src.slice(WIKI.length))} label={alt} context={props} />
          const path = resolveNote(props.path, src, props.index, false)
          return path ? <img src={api.imageUrl(props.project, path, props.version)} alt={alt} loading="lazy" /> : <span className="text-muted">［画像: {alt}］</span>
        },
      }}>{props.text}</ReactMarkdown>
  </div>
}
