import { describe, expect, it } from 'vitest'
import {
  ancestorFolders,
  buildDocTree,
  checkDocPath,
  docMatchScore,
  docPathFromSplat,
  docRoute,
  folderOf,
  fuzzyScore,
  isUnder,
  newDocTemplate,
  titleFromPath,
} from './docTree'
import { splitFrontmatter } from './docFrontmatter'

const doc = (path: string, title = path) => ({ path, title })

describe('buildDocTree (F22)', () => {
  it('derives folders from paths, folders first, the lead file first', () => {
    const root = buildDocTree([
      doc('zeta.md'),
      doc('references/b.md'),
      doc('SKILL.md'),
      doc('references/a.md'),
      doc('references/deep/c.md'),
      doc('alpha.md'),
    ])
    expect(root.count).toBe(6)
    expect(root.folders.map(f => [f.path, f.count])).toEqual([['references/', 3]])
    expect(root.files.map(f => f.name)).toEqual(['SKILL.md', 'alpha.md', 'zeta.md'])
    const refs = root.folders[0]!
    expect(refs.folders.map(f => f.path)).toEqual(['references/deep/'])
    expect(refs.files.map(f => f.path)).toEqual(['references/a.md', 'references/b.md'])
  })

  it('is empty for no notes', () => {
    expect(buildDocTree([])).toMatchObject({ folders: [], files: [], count: 0 })
  })
})

describe('path helpers', () => {
  it('lists the folders a path runs through', () => {
    expect(ancestorFolders('a/b/c.md')).toEqual(['a/', 'a/b/'])
    expect(ancestorFolders('c.md')).toEqual([])
    expect(folderOf('a/b/c.md')).toBe('a/b/')
    expect(folderOf('c.md')).toBe('')
  })

  it('checks prefixes case-insensitively, as paths are unique that way', () => {
    expect(isUnder('Guides/x.md', 'guides/')).toBe(true)
    expect(isUnder('guidesx/x.md', 'guides/')).toBe(false)
    expect(isUnder('x.md', '')).toBe(true)
  })

  it('encodes each segment of a route and keeps the slashes', () => {
    expect(docRoute('demo', 'organization', 'a b/c#1.md')).toBe('/p/demo/docs/organization/a%20b/c%231.md')
    expect(docPathFromSplat('guides/setup.md/')).toBe('guides/setup.md')
    expect(docPathFromSplat(undefined)).toBe('')
  })
})

describe('checkDocPath', () => {
  it('normalises and appends .md', () => {
    expect(checkDocPath('  ./guides//setup ')).toEqual({ ok: true, path: 'guides/setup.md' })
    expect(checkDocPath('SKILL.md')).toEqual({ ok: true, path: 'SKILL.md' })
    expect(checkDocPath('references/api (v2).MD')).toEqual({ ok: true, path: 'references/api (v2).MD' })
    expect(checkDocPath('guides/', { asFolder: true })).toEqual({ ok: true, path: 'guides/' })
  })

  it('rejects traversal, absolute paths, backslashes and bad names', () => {
    expect(checkDocPath('../x').ok).toBe(false)
    expect(checkDocPath('a/./b').ok).toBe(false)
    expect(checkDocPath('/abs').ok).toBe(false)
    expect(checkDocPath('a\\b').ok).toBe(false)
    expect(checkDocPath('.hidden').ok).toBe(false)
    expect(checkDocPath('a\u0000b').ok).toBe(false)
    expect(checkDocPath('').ok).toBe(false)
    expect(checkDocPath(Array.from({ length: 11 }, () => 'a').join('/')).ok).toBe(false)
  })
})

describe('fuzzy matching', () => {
  it('matches subsequences and prefers word starts', () => {
    expect(fuzzyScore('ckout', 'checkout.md')).not.toBeNull()
    expect(fuzzyScore('xyz', 'checkout.md')).toBeNull()
    expect(fuzzyScore('', 'anything')).toBe(0)
    const strong = fuzzyScore('setup', 'guides/setup.md')!
    const weak = fuzzyScore('setup', 'guides/some-extra-thing-useful-paths.md')!
    expect(strong).toBeGreaterThan(weak)
  })

  it('scores a note on its title or its path', () => {
    expect(docMatchScore('funnel', doc('x/y.md', 'Funnel recipes'))).not.toBeNull()
    expect(docMatchScore('recipes', doc('warehouse/recipes.md', 'Gotchas'))).not.toBeNull()
    expect(docMatchScore('zzz', doc('a.md', 'b'))).toBeNull()
  })
})

describe('new notes', () => {
  it('titles a note from its file stem and seeds frontmatter', () => {
    expect(titleFromPath('guides/warehouse-gotchas.md')).toBe('Warehouse gotchas')
    const content = newDocTemplate('recipes/event_queries.md', { body: '[[event:checkout]]' })
    const { frontmatter, body } = splitFrontmatter(content)
    expect(frontmatter).toContain('title: "Event queries"')
    expect(frontmatter).toContain('audience: both')
    expect(body).toContain('# Event queries')
    expect(body).toContain('[[event:checkout]]')
  })
})

describe('splitFrontmatter', () => {
  it('splits a block at the top closed by --- or ...', () => {
    expect(splitFrontmatter('---\ntitle: a\n---\n# Body')).toEqual({ frontmatter: 'title: a\n', body: '# Body' })
    expect(splitFrontmatter('---\nname: x\n...\nrest')).toEqual({ frontmatter: 'name: x\n', body: 'rest' })
  })

  it('leaves a note without a block, or with an unclosed one, whole', () => {
    expect(splitFrontmatter('# Title\n---\n')).toEqual({ frontmatter: null, body: '# Title\n---\n' })
    expect(splitFrontmatter('---\ntitle: a\n')).toEqual({ frontmatter: null, body: '---\ntitle: a\n' })
  })
})
