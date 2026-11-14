import type { NoteIndex } from './api'

export function resolveNote(source: string, target: string, index: NoteIndex, wiki = true): string | null {
  let raw = target.split('#')[0].split('?')[0]
  try { raw = decodeURIComponent(raw) } catch { return null }
  if (!raw) return source
  if (/^[a-z][\w+.-]*:/i.test(raw) || raw.startsWith('//') || raw.includes('\\')) return null
  const parent = source.includes('/') ? source.slice(0, source.lastIndexOf('/') + 1) : ''
  const normalize = (value: string) => {
    const parts: string[] = []
    for (const part of value.split('/')) {
      if (!part || part === '.') continue
      if (part === '..') { if (!parts.length) return null; parts.pop() } else parts.push(part)
    }
    return parts.join('/')
  }
  const paths = new Set(index.paths)
  for (const candidate of wiki ? [raw.replace(/^\//, ''), parent + raw] : [raw.startsWith('/') ? raw.slice(1) : parent + raw, raw]) {
    const path = normalize(candidate)
    if (path === null) continue
    for (const variant of [path, path + '.md', path + '.markdown']) if (paths.has(variant)) return variant
  }
  if (wiki && !raw.includes('/')) {
    const matches = index.paths.filter((path) => path.split('/').pop() === raw || path.split('/').pop()?.replace(/\.(md|markdown)$/i, '') === raw)
    index.notes.forEach((note) => { if (note.aliases.includes(raw)) matches.push(note.path) })
    const unique = [...new Set(matches)]
    if (unique.length === 1) return unique[0]
  }
  return null
}

export function noteSection(text: string, fragment: string): string {
  if (!fragment) return text
  const lines = text.split('\n')
  if (fragment.startsWith('^')) {
    const end = lines.findIndex((line) => line.trimEnd().endsWith(fragment))
    if (end < 0) return '（ブロックが見つかりません）'
    let start = end
    while (start > 0 && lines[start - 1].trim()) start--
    return lines.slice(start, end + 1).join('\n').replace(new RegExp('\\s*\\^' + fragment.slice(1).replace(/[^\w-]/g, '') + '\\s*$'), '')
  }
  const start = lines.findIndex((line) => line.replace(/^#{1,6}\s+/, '').replace(/\s+#+\s*$/, '').trim() === fragment)
  if (start < 0) return '（見出しが見つかりません）'
  const level = lines[start].match(/^#+/)?.[0].length ?? 6
  let end = start + 1
  while (end < lines.length && !(new RegExp(`^#{1,${level}}\\s`).test(lines[end]))) end++
  return lines.slice(start, end).join('\n')
}
