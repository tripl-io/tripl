import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { TooltipProvider } from '@/components/ui/tooltip'
import { termAnchor } from '@/lib/glossary'
import { TERM_HINTS, TermHint } from './term-hint'

describe('TermHint', () => {
  it('links the term to its row in the Concepts glossary', () => {
    render(
      <TooltipProvider>
        <MemoryRouter>
          <TermHint slug="demo" term="Fact tables" definition="A warehouse table a metric reads." />
        </MemoryRouter>
      </TooltipProvider>,
    )

    expect(screen.getByRole('link', { name: 'What is Fact tables? Open in Concepts' })).toHaveAttribute(
      'href',
      '/p/demo/concepts#term-fact-tables',
    )
  })

  it('spells every preset as the glossary does, so its anchor resolves', () => {
    expect(Object.values(TERM_HINTS).map((hint) => termAnchor(hint.term))).toEqual([
      'term-scans',
      'term-fact-tables',
      'term-reconciliation',
      'term-coverage',
      'term-scopes',
    ])
  })
})
