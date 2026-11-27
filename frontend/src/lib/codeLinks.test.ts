import { describe, expect, it } from 'vitest'
import { codeUrl, type CodePlace } from './codeLinks'

const place: CodePlace = {
  repositoryUrl: 'https://git.example.com/shop/web/',
  repository: 'shop/web',
  ref: 'release/2.1',
  commit: 'abc123',
  path: 'src/cart page/Cart#1.ts',
  line: 42,
}

describe('codeUrl', () => {
  it('fills every placeholder and escapes each path segment', () => {
    expect(codeUrl('https://github.com/{repository}/blob/{ref}/{path}#L{line}', place)).toBe(
      'https://github.com/shop/web/blob/release/2.1/src/cart%20page/Cart%231.ts#L42',
    )
    expect(codeUrl('{repository_url}/-/blob/{commit}/{path}#L{line}', place)).toBe(
      'https://git.example.com/shop/web/-/blob/abc123/src/cart%20page/Cart%231.ts#L42',
    )
  })

  it('gives no link without a template', () => {
    expect(codeUrl(null, place)).toBeNull()
    expect(codeUrl('', place)).toBeNull()
  })

  it('gives no link when the run sent no repository address the template needs', () => {
    expect(codeUrl('{repository_url}/blob/{commit}/{path}', { ...place, repositoryUrl: null })).toBeNull()
    expect(
      codeUrl('{repository_url}/blob/{commit}/{path}', { ...place, repositoryUrl: 'javascript:alert(1)' }),
    ).toBeNull()
  })

  it('gives no link for a path that walks out of its directory', () => {
    expect(codeUrl('https://git.example.com/{path}', { ...place, path: '../../admin' })).toBeNull()
    expect(codeUrl('https://git.example.com/{path}', { ...place, path: './src/a.ts' })).toBe(
      'https://git.example.com/src/a.ts',
    )
  })

  it('gives no link for a template that is not http(s) or names an unknown placeholder', () => {
    expect(codeUrl('javascript:alert(1)//{path}', place)).toBeNull()
    expect(codeUrl('https://git.example.com/{branch}/{path}', place)).toBeNull()
  })

  it('keeps the host the template writes out', () => {
    const url = codeUrl('https://git.example.com/{path}', { ...place, path: '@evil.example/x' })
    expect(url).not.toBeNull()
    expect(new URL(url as string).host).toBe('git.example.com')
  })
})
