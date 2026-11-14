import type { DeckSpec } from '../deckSpec'

export function audienceHash(spec: DeckSpec): string {
  return `#/present?${new URLSearchParams({ project: spec.project, path: spec.path, kind: spec.kind, v: String(spec.version) })}`
}

export function audienceSpec(): DeckSpec | null {
  if (!window.location.hash.startsWith('#/present?')) return null
  const query = new URLSearchParams(window.location.hash.slice('#/present?'.length))
  const project = query.get('project'), path = query.get('path'), kind = query.get('kind')
  if (!project || !path || (kind !== 'pdf' && kind !== 'marp')) return null
  return { project, path, kind, version: Number(query.get('v')) || 0 }
}
