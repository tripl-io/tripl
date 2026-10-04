import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { render } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ThemeProvider } from '@/components/theme-provider'
import { SqlEditor } from './sql-editor'

// The theme CodeMirror is handed decides whether it registers as dark: that is
// what switches its base theme's tooltip and highlight colours. jsdom applies no
// stylesheet, so the prop is pinned here and the token rules against the file.
const { themes } = vi.hoisted(() => ({ themes: [] as unknown[] }))
vi.mock('@uiw/react-codemirror', () => ({
  default: ({ theme, 'aria-label': ariaLabel }: { theme?: unknown; 'aria-label'?: string }) => {
    themes.push(theme)
    return <textarea aria-label={ariaLabel} readOnly />
  },
}))

function renderIn(theme: 'dark' | 'light') {
  return render(
    <ThemeProvider defaultTheme={theme} storageKey={`sql-editor-theme-${theme}`}>
      <SqlEditor value="select 1" onChange={() => {}} ariaLabel="Query" />
    </ThemeProvider>,
  )
}

function readIndexCss(): string {
  const testPath = expect.getState().testPath
  if (!testPath) throw new Error('vitest did not report a testPath')
  return readFileSync(resolve(dirname(testPath), '../index.css'), 'utf8')
}

function ruleBody(css: string, selector: RegExp): string {
  const match = css.match(new RegExp(`${selector.source}\\s*\\{([^}]*)\\}`))
  return match?.[1] ?? ''
}

describe('SqlEditor theme', () => {
  beforeEach(() => {
    themes.length = 0
    localStorage.clear()
  })

  it('hands CodeMirror the dark theme when the app is dark', () => {
    renderIn('dark')
    expect(themes[themes.length - 1]).toBe('dark')
  })

  it('hands CodeMirror the light theme when the app is light', () => {
    renderIn('light')
    expect(themes[themes.length - 1]).toBe('light')
  })

  it('paints the autocomplete tooltip from popover tokens, not CodeMirror defaults', () => {
    const css = readIndexCss()
    const tooltip = ruleBody(css, /\.sql-editor\s+\.cm-editor\s+\.cm-tooltip/)
    expect(tooltip).toMatch(/background:\s*var\(--popover\)/)
    expect(tooltip).toMatch(/color:\s*var\(--popover-foreground\)/)

    const selected = ruleBody(
      css,
      /\.sql-editor\s+\.cm-editor\s+\.cm-tooltip-autocomplete\s*>\s*ul\s*>\s*li\[aria-selected\]/,
    )
    expect(selected).toMatch(/background:\s*var\(--surface-hover\)/)
    expect(selected).toMatch(/color:\s*var\(--fg\)/)
  })

  it('draws one frame, on the 3:1 form-control edge', () => {
    const css = readIndexCss()
    const frame = ruleBody(css, /\.sql-editor\s+\.cm-editor/)
    expect(frame).toMatch(/border:\s*1px solid var\(--input\)/)
  })
})
