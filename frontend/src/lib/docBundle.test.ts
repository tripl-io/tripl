import { describe, expect, it } from 'vitest'
import { parseBundleFile } from './docBundle'

describe('parseBundleFile', () => {
  it('reads the files of a bundle, sha256 optional', () => {
    const text = JSON.stringify({
      format: 'tripl-docs/v1',
      files: [
        { path: 'a.md', content: '# A', sha256: 'abc' },
        { path: 'b.md', content: '# B' },
        { path: 'c.md', content: '# C', sha256: 42 },
      ],
    })
    expect(parseBundleFile(text)).toEqual([
      { path: 'a.md', content: '# A', sha256: 'abc' },
      { path: 'b.md', content: '# B', sha256: null },
      { path: 'c.md', content: '# C', sha256: null },
    ])
  })

  it.each([['null'], ['[]'], ['"text"'], ['{"files": {}}'], ['{}']])('rejects %s as not a bundle', text => {
    expect(() => parseBundleFile(text)).toThrow('Not a tripl docs bundle')
  })

  it('names the first entry without a string path and content', () => {
    expect(() => parseBundleFile('{"files": [{"path": "a.md", "content": ""}, {"path": 1, "content": ""}]}')).toThrow(
      'Entry 2 needs a string "path" and "content".',
    )
    expect(() => parseBundleFile('{"files": [null]}')).toThrow('Entry 1')
    expect(() => parseBundleFile('{"files": [{"path": "a.md"}]}')).toThrow('Entry 1')
  })

  it('lets invalid JSON surface as a SyntaxError', () => {
    expect(() => parseBundleFile('{')).toThrow(SyntaxError)
  })
})
