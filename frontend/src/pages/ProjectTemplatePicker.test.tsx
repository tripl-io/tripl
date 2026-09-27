import { useState } from 'react'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { projectTemplatesApi } from '@/api/projectTemplates'
import { ProjectTemplatePicker } from './ProjectTemplatePicker'
import { ecommerceTemplate } from '@/test/projectTemplates'
import { expectNoAxeViolations } from '@/test/axe'
import { SUGGESTIONS_NOTE, formatTemplateCounts, formatTemplateMeta } from './projectTemplateCopy'

vi.mock('@/api/projectTemplates', () => ({
  projectTemplatesApi: { list: vi.fn() },
}))

const listMock = vi.mocked(projectTemplatesApi.list)

const templateFixture = ecommerceTemplate

const SUBSCRIPTIONS = templateFixture({
  id: 'subscriptions',
  name: 'Subscriptions',
  description: 'Trials, renewals and cancellations.',
  branch_name: 'template/subscriptions',
  counts: {
    event_types: 1,
    fields: 3,
    events: 1,
    variables: 1,
    metric_suggestions: 0,
    alert_suggestions: 0,
  },
  metric_suggestions: [],
  alert_suggestions: [],
})

function Harness({ initial = null }: { initial?: string | null }) {
  const [value, setValue] = useState<string | null>(initial)
  return (
    <>
      <ProjectTemplatePicker value={value} onChange={setValue} />
      <output data-testid="value">{value ?? 'blank'}</output>
    </>
  )
}

function renderPicker(initial: string | null = null) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Harness initial={initial} />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.clearAllMocks()
})

describe('formatTemplateCounts', () => {
  it('reads events, event types and variables with plurals', () => {
    expect(formatTemplateCounts(templateFixture().counts)).toBe(
      '9 events · 5 event types · 4 variables',
    )
    expect(formatTemplateCounts(SUBSCRIPTIONS.counts)).toBe('1 event · 1 event type · 1 variable')
  })

  it('appends the template version for the card', () => {
    expect(formatTemplateMeta(templateFixture())).toBe(
      '9 events · 5 event types · 4 variables · version 1',
    )
  })
})

describe('ProjectTemplatePicker', () => {
  it('offers Blank project first and checked, then one card per template', async () => {
    listMock.mockResolvedValue([templateFixture(), SUBSCRIPTIONS])
    renderPicker()

    const group = screen.getByRole('radiogroup', { name: 'Start from' })
    expect(group).toBeInTheDocument()
    await screen.findByRole('radio', { name: 'E-commerce' })

    const radios = screen.getAllByRole('radio')
    expect(radios.map((radio) => radio.getAttribute('aria-checked'))).toEqual([
      'true',
      'false',
      'false',
    ])
    expect(radios[0]).toHaveAccessibleName('Blank project')
    const ecommerce = screen.getByRole('radio', { name: 'E-commerce' })
    expect(ecommerce).toHaveAccessibleDescription(
      `${templateFixture().description} 9 events · 5 event types · 4 variables · version 1`,
    )
    // One Tab stop for the whole group, on the checked card.
    expect(radios[0]).toHaveAttribute('tabindex', '0')
    expect(ecommerce).toHaveAttribute('tabindex', '-1')
  })

  it('selects by click and moves the choice with arrow, Home and End keys', async () => {
    listMock.mockResolvedValue([templateFixture(), SUBSCRIPTIONS])
    renderPicker()
    const ecommerce = await screen.findByRole('radio', { name: 'E-commerce' })

    fireEvent.click(ecommerce)
    expect(screen.getByTestId('value')).toHaveTextContent('ecommerce')
    expect(ecommerce).toHaveAttribute('aria-checked', 'true')
    expect(ecommerce).toHaveAttribute('tabindex', '0')

    fireEvent.keyDown(ecommerce, { key: 'ArrowDown' })
    const subscriptions = screen.getByRole('radio', { name: 'Subscriptions' })
    expect(screen.getByTestId('value')).toHaveTextContent('subscriptions')
    expect(subscriptions).toHaveFocus()

    // Wraps around to Blank project.
    fireEvent.keyDown(subscriptions, { key: 'ArrowDown' })
    const blank = screen.getByRole('radio', { name: 'Blank project' })
    expect(screen.getByTestId('value')).toHaveTextContent('blank')
    expect(blank).toHaveFocus()

    fireEvent.keyDown(blank, { key: 'ArrowUp' })
    expect(screen.getByTestId('value')).toHaveTextContent('subscriptions')

    fireEvent.keyDown(screen.getByRole('radio', { name: 'Subscriptions' }), { key: 'Home' })
    expect(screen.getByTestId('value')).toHaveTextContent('blank')

    fireEvent.keyDown(screen.getByRole('radio', { name: 'Blank project' }), { key: 'End' })
    expect(screen.getByTestId('value')).toHaveTextContent('subscriptions')
  })

  it('lists the checked template\'s starter metrics and alerts as suggestions', async () => {
    listMock.mockResolvedValue([templateFixture(), SUBSCRIPTIONS])
    renderPicker()
    await screen.findByRole('radio', { name: 'E-commerce' })

    // Blank project has nothing to suggest.
    expect(
      screen.queryByRole('button', { name: /Starter metrics and alerts/ }),
    ).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('radio', { name: 'E-commerce' }))
    const toggle = screen.getByRole('button', { name: /Starter metrics and alerts/ })
    expect(toggle).toHaveAccessibleName('Starter metrics and alerts for E-commerce')
    expect(toggle).toHaveAccessibleDescription(SUGGESTIONS_NOTE)
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    // The disclosure is not inside the radiogroup, which owns only radios.
    expect(screen.getByRole('radiogroup')).not.toContainElement(toggle)
    expect(screen.getByText(SUGGESTIONS_NOTE)).toHaveTextContent(/not added automatically/)
    expect(screen.queryByText('Checkout conversion')).not.toBeInTheDocument()

    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('Checkout conversion')).toBeInTheDocument()
    expect(screen.getByText('(needs a scan)')).toBeInTheDocument()
    expect(screen.getByText('core_funnel_volume_drop')).toBeInTheDocument()
    expect(screen.getByText('(needs an alert destination)')).toBeInTheDocument()

    // Opening the list does not change the choice.
    expect(screen.getByTestId('value')).toHaveTextContent('ecommerce')

    // A template without suggestions gets no disclosure.
    fireEvent.click(screen.getByRole('radio', { name: 'Subscriptions' }))
    expect(
      screen.queryByRole('button', { name: /Starter metrics and alerts/ }),
    ).not.toBeInTheDocument()
  })

  it('has no axe violations with a template and its suggestions shown', async () => {
    listMock.mockResolvedValue([templateFixture(), SUBSCRIPTIONS])
    const { container } = renderPicker('ecommerce')
    await screen.findByRole('radio', { name: 'E-commerce' })
    fireEvent.click(screen.getByRole('button', { name: /Starter metrics and alerts/ }))

    await expectNoAxeViolations(container)
  })

  it('says it is loading while the list is on its way', () => {
    listMock.mockReturnValue(new Promise(() => {}))
    renderPicker()

    expect(screen.getByRole('status')).toHaveTextContent('Loading templates…')
    expect(screen.getAllByRole('radio')).toHaveLength(1)
    expect(screen.getByRole('radiogroup')).toHaveAttribute('aria-busy', 'true')
  })

  it('falls back to Blank project only when the list fails to load', async () => {
    listMock.mockRejectedValue(new Error('boom'))
    renderPicker()

    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent(
        'Templates could not be loaded. You can still start with a blank project.',
      ),
    )
    const radios = screen.getAllByRole('radio')
    expect(radios).toHaveLength(1)
    expect(radios[0]).toHaveAccessibleName('Blank project')
    expect(radios[0]).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByRole('radiogroup')).toHaveAccessibleDescription(
      /Templates could not be loaded/,
    )
  })

  it('keeps Blank project as the one choice when there are no templates', async () => {
    listMock.mockResolvedValue([])
    renderPicker()

    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent('No templates are available.'),
    )
    expect(screen.getAllByRole('radio')).toHaveLength(1)
  })
})
