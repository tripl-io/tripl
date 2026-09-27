import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import type { DocLinkResolution } from '@/types/docs'
import { DocMarkdown } from './DocMarkdown'
import { docUrlTransform } from './docMarkdownUrl'

function resolution(overrides: Partial<DocLinkResolution>): DocLinkResolution {
  return {
    kind: 'event',
    target: 'checkout_started',
    qualifier: null,
    raw: '[[event:checkout_started]]',
    status: 'resolved',
    route_path: '/p/demo/monitoring/event/e-1',
    entity_id: 'e-1',
    candidates: 1,
    ...overrides,
  }
}

function renderMarkdown(body: string, resolutions: DocLinkResolution[] | undefined = []) {
  return render(
    <MemoryRouter>
      <DocMarkdown body={body} slug="demo" scope="project" path="guides/setup.md" resolutions={resolutions} />
    </MemoryRouter>,
  )
}

describe('DocMarkdown (F22)', () => {
  it('renders GFM: headings, tables and task lists', () => {
    renderMarkdown('# Title\n\n| a | b |\n| - | - |\n| 1 | 2 |\n\n- [x] done\n')
    expect(screen.getByRole('heading', { name: 'Title', level: 1 })).toBeInTheDocument()
    expect(screen.getByRole('table')).toBeInTheDocument()
    expect(screen.getByRole('checkbox')).toBeChecked()
  })

  it('never renders raw HTML: a script or an onerror image stays text', () => {
    const { container } = renderMarkdown('<script>alert(1)</script>\n\n<img src=x onerror="alert(2)">\n')
    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('img')).toBeNull()
  })

  it('does not fetch Markdown images; it shows them as links', () => {
    const { container } = renderMarkdown('![a chart](https://example.com/c.png)')
    expect(container.querySelector('img')).toBeNull()
    expect(screen.getByRole('link', { name: '[image: a chart]' })).toHaveAttribute('href', 'https://example.com/c.png')
  })

  it('turns a resolved entity link into an in-app link', () => {
    renderMarkdown('See [[event:checkout_started|the start event]].', [resolution({})])
    expect(screen.getByRole('link', { name: 'the start event' })).toHaveAttribute(
      'href',
      '/p/demo/monitoring/event/e-1',
    )
  })

  it('marks a broken link and says why', () => {
    const { container } = renderMarkdown('[[event:gone]]', [
      resolution({ target: 'gone', raw: '[[event:gone]]', status: 'broken', route_path: null, entity_id: null, candidates: 0 }),
    ])
    const chip = container.querySelector('[data-doc-link="broken"]')
    expect(chip).not.toBeNull()
    expect(chip).toHaveAttribute('title', "[[event:gone]]: no event named 'gone' on the main plan.")
    expect(screen.queryByRole('link')).toBeNull()
  })

  it('renders a link neutral while resolutions load', () => {
    const { container } = renderMarkdown('[[field:checkout/amount]]', undefined)
    expect(container.querySelector('[data-doc-link="pending"]')).toHaveTextContent('checkout/amount')
  })

  it('leaves links inside code alone', () => {
    const { container } = renderMarkdown('`[[event:checkout_started]]`', [resolution({})])
    expect(container.querySelector('code')).toHaveTextContent('[[event:checkout_started]]')
    expect(screen.queryByRole('link')).toBeNull()
  })

  it('opens a relative .md link as a note in the same scope', () => {
    renderMarkdown('[install](../references/install.md#steps)')
    expect(screen.getByRole('link', { name: 'install' })).toHaveAttribute(
      'href',
      '/p/demo/docs/project/references/install.md#steps',
    )
  })

  it('opens external links in a new tab without an opener', () => {
    renderMarkdown('[docs](https://example.com/guide)')
    const link = screen.getByRole('link', { name: /docs/ })
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer nofollow')
  })

  it('keeps tripl: hrefs and still strips javascript: URLs', () => {
    expect(docUrlTransform('tripl:event/a')).toBe('tripl:event/a')
    expect(docUrlTransform('javascript:alert(1)')).toBe('')
  })
})
