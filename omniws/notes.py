"""Bounded, read-only Markdown index, using the file browser's access checks."""
from __future__ import annotations

import posixpath
import re
from pathlib import PurePosixPath
from urllib.parse import unquote

from markdown_it import MarkdownIt
import yaml

from .files import EDIT_LIMIT, FileError, ProjectFiles

MAX_NOTES = 2000
MAX_ENTRIES = 10000
MAX_BYTES = 16 * 1024 * 1024
WIKI = re.compile(r'!?\[\[([^\]\n]+)\]\]')
TAG = re.compile(r'(?<![\w/#])#([^\W\d][\w/-]*)', re.UNICODE)
MARKDOWN = {'.md', '.markdown'}


def metadata(text: str) -> tuple[dict, str]:
    match = re.match(r'\A---\r?\n(.*?)\r?\n(?:---|\.\.\.)\s*(?:\r?\n|$)', text, re.S)
    if not match:
        return {}, text
    try:
        value = yaml.safe_load(match[1]) if len(match[1]) <= 65536 else None
    except (yaml.YAMLError, RecursionError):
        value = None
    return value if isinstance(value, dict) else {}, text[match.end():]


def strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def parse(path: str, text: str) -> dict:
    meta, body = metadata(text)
    links, tags, headings = [], set(strings(meta.get('tags'))), []
    tokens = MarkdownIt().parse(body)
    title = PurePosixPath(path).stem
    for index, token in enumerate(tokens):
        if token.type != 'inline':
            continue
        if index and tokens[index - 1].type == 'heading_open':
            headings.append(token.content)
            if len(headings) == 1:
                title = token.content
        for child in token.children or []:
            if child.type == 'text':
                links.extend({'target': m[1].split('|', 1)[0], 'wiki': True} for m in WIKI.finditer(child.content))
                tags.update(TAG.findall(WIKI.sub('', child.content)))
            elif child.type == 'link_open':
                links.append({'target': child.attrGet('href') or '', 'wiki': False})
            elif child.type == 'image':
                links.append({'target': child.attrGet('src') or '', 'wiki': False})
    return {'path': path, 'title': str(meta.get('title')) if isinstance(meta.get('title'), str) else title,
            'aliases': strings(meta.get('aliases')), 'tags': sorted(tags), 'headings': headings,
            'links': links, 'text': text}


def resolve(source: str, target: str, paths: set[str], aliases: dict[str, list[str]], wiki: bool = True) -> str | None:
    target = unquote(target.split('#', 1)[0].split('?', 1)[0])
    if not target:
        return source
    if re.match(r'^[a-zA-Z][\w+.-]*:', target) or target.startswith('//') or '\\' in target:
        return None
    candidates = [target.lstrip('/'), posixpath.join(posixpath.dirname(source), target)] if wiki else [posixpath.join(posixpath.dirname(source), target), target.lstrip('/')]
    for candidate in candidates:
        candidate = posixpath.normpath(candidate)
        if candidate.startswith('../') or candidate == '..':
            continue
        for variant in (candidate, candidate + '.md', candidate + '.markdown'):
            if variant in paths:
                return variant
    if wiki and '/' not in target:
        matches = set(aliases.get(target, []))
        matches.update(path for path in paths if PurePosixPath(path).name == target or PurePosixPath(path).stem == target)
        if len(matches) == 1:
            return matches.pop()
    return None


def index(settings, project: str, query: str = '', current: str = '') -> dict:
    files = ProjectFiles(settings, project)
    pending, notes, paths = [''], [], set()
    size = count = 0
    truncated = False
    while pending and not truncated:
        folder = pending.pop()
        offset = 0
        while True:
            try:
                tree = files.tree(folder, offset, 500)
            except FileError:
                break
            for entry in tree['entries']:
                count += 1
                if count > MAX_ENTRIES or len(notes) >= MAX_NOTES:
                    truncated = True
                    break
                path = entry['path']
                if entry['kind'] == 'directory':
                    if entry['name'] not in {'node_modules', '.venv', '__pycache__', '.obsidian'} and path.count('/') < 32:
                        pending.append(path)
                    continue
                if entry['kind'] != 'file':
                    continue
                paths.add(path)
                if PurePosixPath(path).suffix.lower() not in MARKDOWN:
                    continue
                if size + entry['size'] > MAX_BYTES:
                    truncated = True
                    break
                try:
                    data = files.file(path, EDIT_LIMIT)
                except FileError:
                    continue
                size += entry['size']
                if data['kind'] == 'text' and not data['truncated']:
                    notes.append(parse(path, data['text']))
            offset = tree['next_offset']
            if offset is None or truncated:
                break
    aliases: dict[str, list[str]] = {}
    for note in notes:
        for alias in note['aliases']:
            aliases.setdefault(alias, []).append(note['path'])
    backlinks, results = [], []
    needle = query.casefold().strip()
    for note in notes:
        links = {resolve(note['path'], link['target'], paths, aliases, link['wiki']) for link in note['links']}
        if current and current in links and current != note['path']:
            backlinks.append({'path': note['path'], 'title': note['title']})
        haystack = '\n'.join([note['path'], note['title'], *note['aliases'], *note['tags'], note['text']]).casefold()
        if needle and needle in haystack and len(results) < 100:
            lines = note['text'].splitlines()
            match = next(((i + 1, line) for i, line in enumerate(lines) if needle in line.casefold()), (1, note['title']))
            results.append({'path': note['path'], 'title': note['title'], 'line': match[0], 'snippet': match[1][:240]})
    summaries = [{key: note[key] for key in ('path', 'title', 'aliases', 'tags', 'headings')} for note in notes]
    return {'notes': sorted(summaries, key=lambda item: item['path']), 'paths': sorted(paths),
            'backlinks': backlinks, 'results': results, 'truncated': truncated}
