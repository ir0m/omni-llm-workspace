/** What a slide deck is made from: a PDF (a project file, or the build of a .typ / .tex document) or Marp Markdown. */
export type DeckSpec = { project: string; path: string; kind: 'pdf' | 'marp'; version: number }
/** `marp: true` in the front matter makes a Markdown file a slide deck (the Marp convention). */
export function isMarp(text: string): boolean {
  const match = text.match(/^---\r?\n([\s\S]*?)\r?\n---/)
  return !!match && /^marp:\s*true\s*$/m.test(match[1])
}
