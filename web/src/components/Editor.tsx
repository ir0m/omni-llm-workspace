import { useEffect, useRef } from 'react'
import { basicSetup } from 'codemirror'
import { autocompletion } from '@codemirror/autocomplete'
import { Compartment, EditorState, type Extension } from '@codemirror/state'
import { EditorView, keymap } from '@codemirror/view'
import { indentWithTab } from '@codemirror/commands'
import { StreamLanguage, syntaxHighlighting } from '@codemirror/language'
import { markdown } from '@codemirror/lang-markdown'
import { stex } from '@codemirror/legacy-modes/mode/stex'
import { classHighlighter } from '@lezer/highlight'

// Colors come from the app's tokens (index.css), so the editor follows light and dark like the rest of the screen.
const theme = EditorView.theme({
  '&': { height: '100%', fontSize: '14px', backgroundColor: 'var(--color-bg)', color: 'var(--color-fg)' },
  '&.cm-focused': { outline: 'none' },
  '.cm-scroller': { fontFamily: 'var(--font-mono)', lineHeight: '1.6' },
  '.cm-content': { caretColor: 'var(--color-fg)', padding: '12px 0' },
  '.cm-cursor': { borderLeftColor: 'var(--color-fg)' },
  '.cm-gutters': { backgroundColor: 'var(--color-surface)', color: 'var(--color-muted)', border: 'none' },
  '.cm-activeLine': { backgroundColor: 'color-mix(in srgb, var(--color-raised) 55%, transparent)' },
  '.cm-activeLineGutter': { backgroundColor: 'var(--color-raised)' },
  '&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground, .cm-selectionBackground, ::selection':
    { backgroundColor: 'color-mix(in srgb, var(--color-accent) 28%, transparent)' },
  '.cm-panels': { backgroundColor: 'var(--color-surface)', color: 'var(--color-fg)' },
  '.cm-tooltip': { backgroundColor: 'var(--color-surface)', color: 'var(--color-fg)', border: '1px solid var(--color-border)' },
})

function language(path: string): Extension[] {
  const extension = path.slice(path.lastIndexOf('.') + 1).toLowerCase()
  if (['md', 'markdown', 'mdx'].includes(extension)) return [markdown()]
  if (['tex', 'sty', 'cls', 'bib'].includes(extension)) return [StreamLanguage.define(stex)]
  return []
}

const PROSE = /\.(md|markdown|mdx|tex|typ|txt)$/i

/**
 * A CodeMirror editor for one loaded file. `docKey` identifies the loaded content: the editor is rebuilt when it
 * changes (another file, or the same file read again from the server) and keeps its own text in between.
 */
export default function Editor({ docKey, path, text, readOnly, jump, notePaths = [], onChange, onSave }: {
  docKey: string; path: string; text: string; readOnly: boolean
  /** Move the cursor to this line; a new `tick` repeats a jump to the same line */
  jump?: { line: number; tick: number } | null
  notePaths?: string[]
  onChange: (text: string) => void; onSave: () => void
}) {
  const host = useRef<HTMLDivElement>(null)
  const permission = useRef(new Compartment())
  const editor = useRef<EditorView | null>(null)
  const paths = useRef(notePaths)
  paths.current = notePaths
  const callbacks = useRef({ onChange, onSave })
  callbacks.current = { onChange, onSave }

  useEffect(() => {
    const view = new EditorView({
      parent: host.current!,
      state: EditorState.create({
        doc: text,
        extensions: [
          keymap.of([{ key: 'Mod-s', preventDefault: true, run: () => { callbacks.current.onSave(); return true } }, indentWithTab]),
          basicSetup,
          syntaxHighlighting(classHighlighter),
          theme,
          ...language(path),
          ...(/\.(md|markdown)$/i.test(path) ? [autocompletion({ override: [(context) => {
            const match = context.matchBefore(/\[\[[^\]\n]*$/)
            if (!match) return null
            return { from: match.from + 2, options: paths.current.map((name) => ({ label: name.replace(/\.(md|markdown)$/i, ''), type: 'text',
              apply: (view, _completion, from, to) => {
                const suffix = view.state.doc.sliceString(to, to + 2) === ']]' ? '' : ']]'
                const insert = name.replace(/\.(md|markdown)$/i, '') + suffix
                view.dispatch({ changes: { from, to, insert }, selection: { anchor: from + insert.length } })
              } })), validFor: /^[^\]\n]*$/ }
          }] })] : []),
          ...(PROSE.test(path) ? [EditorView.lineWrapping] : []),
          permission.current.of(EditorState.readOnly.of(readOnly)),
          EditorView.contentAttributes.of({ 'aria-label': `${path} の編集` }),
          EditorView.updateListener.of((update) => { if (update.docChanged) callbacks.current.onChange(update.state.doc.toString()) }),
        ],
      }),
    })
    editor.current = view
    return () => { view.destroy(); editor.current = null }
    // `text` is only the starting content for this docKey; later edits live in the editor itself.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docKey])

  useEffect(() => {
    editor.current?.dispatch({ effects: permission.current.reconfigure(EditorState.readOnly.of(readOnly)) })
  }, [readOnly])

  useEffect(() => {
    const view = editor.current
    if (!view || !jump) return
    const line = view.state.doc.line(Math.max(1, Math.min(jump.line, view.state.doc.lines)))
    view.dispatch({ selection: { anchor: line.from }, effects: EditorView.scrollIntoView(line.from, { y: 'center' }) })
    view.focus()
  }, [jump, docKey])

  return <div ref={host} className="h-full min-h-0 overflow-hidden" />
}
