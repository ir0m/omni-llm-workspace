/**
 * Model output → Markdown that remark-math reads safely.
 *
 * - `\(…\)` and `\[…\]` (common in OpenAI-style output) become `$…$` and `$$…$$`.
 * - A single `$` counts as math only under Pandoc's rule: the opening `$` is followed by a non-space,
 *   the closing `$` follows a non-space and is not followed by a digit. Any other `$` is escaped, so
 *   prices such as "$5 and $10" stay text.
 * Code spans and fenced code blocks are left untouched.
 */

const FENCE = /^ {0,3}(`{3,}|~{3,})/

/** Quick check so plain messages skip loading the math renderer. */
export function mayHaveMath(text: string): boolean {
  return /\$|\\\(|\\\[/.test(text)
}

export function prepareMath(text: string): string {
  const lines = text.split('\n')
  const out: string[] = []
  let prose: string[] = []
  let fence: string | null = null
  const flush = () => {
    if (prose.length) out.push(prepareProse(prose.join('\n')))
    prose = []
  }
  for (const line of lines) {
    const match = FENCE.exec(line)
    if (fence) {
      out.push(line)
      if (match && match[1][0] === fence[0] && match[1].length >= fence.length && !line.slice(match[0].length).trim()) fence = null
    } else if (match) {
      flush()
      fence = match[1]
      out.push(line)
    } else {
      prose.push(line)
    }
  }
  flush()
  return out.join('\n')
}

/** Text outside fenced blocks: protect inline code spans, convert the rest. */
function prepareProse(text: string): string {
  let result = ''
  let at = 0
  const spans = /(`+)[\s\S]*?[^`]\1(?!`)|(`+)(?!`)/g
  for (let match = spans.exec(text); match; match = spans.exec(text)) {
    if (match[2] !== undefined) continue // unmatched backticks are literal text
    result += convert(text.slice(at, match.index)) + match[0]
    at = match.index + match[0].length
  }
  return result + convert(text.slice(at))
}

function convert(text: string): string {
  // Display and inline LaTeX delimiters; the content is kept verbatim.
  const parts: string[] = []
  const delimiters = /\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|\$\$([\s\S]+?)\$\$/g
  let at = 0
  for (let match = delimiters.exec(text); match; match = delimiters.exec(text)) {
    if (isEscaped(text, match.index)) continue
    parts.push(dollars(text.slice(at, match.index)))
    if (match[1] !== undefined) parts.push(`\n$$\n${match[1].trim()}\n$$\n`)
    else if (match[2] !== undefined) parts.push(`$${match[2].trim()}$`)
    else parts.push(match[0])
    at = match.index + match[0].length
  }
  parts.push(dollars(text.slice(at)))
  return parts.join('')
}

function isEscaped(text: string, index: number): boolean {
  let count = 0
  for (let i = index - 1; i >= 0 && text[i] === '\\'; i--) count++
  return count % 2 === 1
}

/** Keep `$…$` pairs that follow Pandoc's rule; escape every other single `$`. */
function dollars(text: string): string {
  let result = ''
  let i = 0
  while (i < text.length) {
    const open = text.indexOf('$', i)
    if (open < 0) break
    result += text.slice(i, open)
    if (isEscaped(text, open)) {
      result += '$'
      i = open + 1
      continue
    }
    const close = closing(text, open)
    if (close < 0) {
      result += '\\$'
      i = open + 1
    } else {
      result += text.slice(open, close + 1)
      i = close + 1
    }
  }
  return result + text.slice(i)
}

function closing(text: string, open: number): number {
  const next = text[open + 1]
  if (next === undefined || /\s/.test(next) || next === '$') return -1
  for (let i = open + 1; i < text.length; i++) {
    const c = text[i]
    if (c === '\n' && text[i + 1] === '\n') return -1 // math does not span paragraphs
    if (c !== '$' || isEscaped(text, i)) continue
    if (/\s/.test(text[i - 1]) || /\d/.test(text[i + 1] ?? '')) return -1
    return i
  }
  return -1
}
