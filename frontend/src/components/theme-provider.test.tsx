import { render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { ThemeProvider, useTheme } from './theme-provider'

function Probe() {
  const { resolvedTheme } = useTheme()
  return <span data-testid="resolved">{resolvedTheme}</span>
}

afterEach(() => {
  localStorage.clear()
  document.documentElement.classList.remove('light', 'dark')
  document.documentElement.style.colorScheme = ''
})

describe('ThemeProvider color-scheme', () => {
  it('sets the root color-scheme to the app theme, not the OS one', () => {
    const { getByTestId, unmount } = render(
      <ThemeProvider defaultTheme="light" storageKey="theme-test-light">
        <Probe />
      </ThemeProvider>,
    )
    expect(document.documentElement).toHaveClass('light')
    expect(document.documentElement.style.colorScheme).toBe('light')
    expect(getByTestId('resolved')).toHaveTextContent('light')
    unmount()

    render(
      <ThemeProvider defaultTheme="dark" storageKey="theme-test-dark">
        <Probe />
      </ThemeProvider>,
    )
    expect(document.documentElement).toHaveClass('dark')
    expect(document.documentElement.style.colorScheme).toBe('dark')
  })

  it('resolves "system" from the OS preference', () => {
    // test-setup's matchMedia answers "no match": the OS prefers light.
    const { getByTestId } = render(
      <ThemeProvider defaultTheme="system" storageKey="theme-test-system">
        <Probe />
      </ThemeProvider>,
    )
    expect(getByTestId('resolved')).toHaveTextContent('light')
    expect(document.documentElement.style.colorScheme).toBe('light')
  })
})

describe('ThemeProvider accent', () => {
  afterEach(() => {
    document.documentElement.className = ''
  })

  it.each([
    ['amber', 'indigo'],
    ['rose', 'magenta'],
  ])('moves a stored retired accent %s to %s', (retired, replacement) => {
    localStorage.setItem('theme-test-accent-accent', retired)
    render(
      <ThemeProvider defaultTheme="light" storageKey="theme-test-accent">
        <Probe />
      </ThemeProvider>,
    )
    expect(document.documentElement).toHaveClass(`accent-${replacement}`)
    expect(document.documentElement).not.toHaveClass(`accent-${retired}`)
    expect(localStorage.getItem('theme-test-accent-accent')).toBe(replacement)
  })

  it('falls back to the default for an unknown stored accent', () => {
    localStorage.setItem('theme-test-accent-accent', 'constructor')
    render(
      <ThemeProvider defaultTheme="light" storageKey="theme-test-accent">
        <Probe />
      </ThemeProvider>,
    )
    expect(document.documentElement).toHaveClass('accent-teal')
    expect(localStorage.getItem('theme-test-accent-accent')).toBe('constructor')
  })
})
