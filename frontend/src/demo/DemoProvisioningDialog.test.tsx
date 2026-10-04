import { act, render, screen, fireEvent } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/client'
import { DemoProvisioningDialog } from './DemoProvisioningDialog'
import { DEMO_PROVISION_SLOW_MS } from './provisioningPhases'
import { expectNoAxeViolations } from '@/test/axe'

function renderDialog(props: Partial<React.ComponentProps<typeof DemoProvisioningDialog>> = {}) {
  return render(
    <DemoProvisioningDialog
      status="provisioning"
      phaseIndex={0}
      error={null}
      timedOut={false}
      onRetry={() => {}}
      onCancel={() => {}}
      onClose={() => {}}
      {...props}
    />,
  )
}

describe('DemoProvisioningDialog', () => {
  it('shows staged progress and announces the current phase (a11y)', () => {
    renderDialog({ phaseIndex: 1 })

    // Every expected phase is listed…
    expect(screen.getByText('Creating workspace')).toBeInTheDocument()
    expect(screen.getByText('Seeding events')).toBeInTheDocument()
    expect(screen.getByText('Finalizing')).toBeInTheDocument()
    // …and the live region narrates the active one for screen readers.
    const live = screen.getByRole('status')
    expect(live).toHaveTextContent('Seeding events')
  })

  it('lets the user abandon an in-flight create', () => {
    // The dialog used to be un-dismissable while pending, so a stalled request
    // left a page reload as the only way out. It is abandonable now: both the
    // close (X) button and the footer Cancel abort the request.
    const onCancel = vi.fn()
    renderDialog({ onCancel })

    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }))
    expect(onCancel).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: /close/i }))
    expect(onCancel).toHaveBeenCalledTimes(2)
  })

  it('renders a human error with a Retry action on 500', () => {
    const onRetry = vi.fn()
    renderDialog({
      status: 'error',
      error: new ApiError('Demo provisioning failed and was rolled back.', 500),
      onRetry,
    })

    expect(screen.getByRole('alert')).toHaveTextContent('rolled back')
    fireEvent.click(screen.getByRole('button', { name: /try again/i }))
    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it('says a 409 is the demo limit, claims no rollback and offers no retry', () => {
    const detail =
      'You already have 3 demo workspaces (the limit is 3). Reset or delete one before generating another.'
    renderDialog({ status: 'error', error: new ApiError(detail, 409) })

    expect(screen.getByRole('heading', { name: 'Demo limit reached' })).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent(detail)
    expect(screen.queryByText(/rolled back/i)).not.toBeInTheDocument()
    // Asking again gets the same answer.
    expect(screen.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /^close$/i }).length).toBeGreaterThan(0)
  })

  it('says a 403 is a refusal, not a failed seed, and lets the server say why', () => {
    // The 403 an editor meets is this one: the button is offered to editors
    // only, so "you need editor access" would be false.
    renderDialog({ status: 'error', error: new ApiError('Demo provisioning is disabled', 403) })

    expect(screen.getByRole('heading', { name: 'Demo workspace not available' })).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('Demo provisioning is disabled')
    expect(screen.queryByText(/editor access/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/rolled back/i)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument()
  })

  it('surfaces the backend request id as a support reference on failure (.15)', () => {
    // The 500 carries a request id (echoed on the response header -> ApiError);
    // the dialog shows it so the user can quote it to support.
    renderDialog({
      status: 'error',
      error: new ApiError('Demo provisioning failed', 500, 'req-abc123'),
    })

    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('Demo provisioning failed')
    expect(alert).toHaveTextContent('Reference: req-abc123')
  })

  it('announces the failure once — no duplicate title/status/alert copy (.15)', () => {
    // Regression: the failure sentence used to appear in the title, a polite
    // status region, AND the alert, so screen readers read it repeatedly. Now
    // the assertive alert is the only live region on the error path.
    renderDialog({
      status: 'error',
      error: new ApiError('Demo provisioning failed and was rolled back.', 500),
    })

    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('rolled back')
  })

  it('says so visibly on success and offers a positive action', () => {
    // Regression: the success frame kept the "Generating demo workspace" title
    // and a greyed-out Cancel; the only "ready" sentence was sr-only.
    renderDialog({ status: 'success', phaseIndex: 4 })

    expect(screen.getByText('Demo workspace is ready')).toBeInTheDocument()
    expect(screen.queryByText('Generating demo workspace')).not.toBeInTheDocument()
    const open = screen.getByRole('button', { name: /open demo/i })
    expect(open).toBeEnabled()
    expect(screen.queryByRole('button', { name: /^cancel$/i })).not.toBeInTheDocument()
  })

  it('labels the phase list as an estimate while the request is open', () => {
    renderDialog({ phaseIndex: 3 })
    expect(screen.getByText(/estimated steps/i)).toBeInTheDocument()
  })

  it('confirms a cancel that actually stopped the create', () => {
    renderDialog({ status: 'cancelled', cancelOutcome: 'stopped' })

    expect(screen.getByText('Demo generation cancelled')).toBeInTheDocument()
    // Rendered both visibly and in the polite live region, hence getAllByText.
    expect(screen.getAllByText(/nothing was added to your projects/i).length).toBeGreaterThan(0)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('admits when a cancel arrived too late instead of implying a rollback', () => {
    renderDialog({ status: 'cancelled', cancelOutcome: 'already-finished' })

    expect(screen.getByText('Too late to cancel')).toBeInTheDocument()
    expect(screen.getAllByText(/will appear in your projects list/i).length).toBeGreaterThan(0)
  })

  it('does not claim a rollback when the request merely timed out', () => {
    // A timeout aborts OUR request; the server may still be seeding. Promising
    // "nothing was left behind" here would be a lie.
    renderDialog({
      status: 'error',
      timedOut: true,
      error: new ApiError('Request to the backend timed out.', 408),
    })

    expect(screen.getByText(/may still be finishing on the server/i)).toBeInTheDocument()
    expect(screen.queryByText(/rolled back/i)).not.toBeInTheDocument()
    // One click on "Try again" could create a duplicate that counts towards
    // the cap; the way on is the projects list behind the dialog.
    expect(screen.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /^close$/i }).length).toBeGreaterThan(0)
  })
})

describe('DemoProvisioningDialog — copy that matches the failure', () => {
  it('does not claim a rollback when the server could not be reached', () => {
    // The client maps a network failure to 503: the server may have accepted
    // the create and finished it.
    renderDialog({
      status: 'error',
      error: new ApiError('Backend unavailable', 503),
    })

    expect(screen.getByText(/the demo may still be created/i)).toBeInTheDocument()
    expect(screen.queryByText(/rolled back/i)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument()
  })

  it('keeps the rollback copy for the server\'s own 500', () => {
    renderDialog({ status: 'error', error: new ApiError('Demo provisioning failed', 500) })

    expect(screen.getByText(/rolled back/i)).toBeInTheDocument()
  })

  it('does not promise a demo "will appear" when the outcome of a cancel is unknown', () => {
    renderDialog({ status: 'cancelled', cancelOutcome: 'unknown' })

    // Neutral: the likeliest case is a demo that finished, not one that stopped.
    expect(screen.getByRole('heading', { name: 'Nothing left to cancel' })).toBeInTheDocument()
    expect(screen.queryByText(/stopped/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/will appear/i)).not.toBeInTheDocument()
    expect(screen.queryByText('Too late to cancel')).not.toBeInTheDocument()
  })

  it('says a create is taking longer than usual once it is well past the estimate', () => {
    vi.useFakeTimers()
    try {
      renderDialog()
      expect(screen.queryByText(/taking longer than usual/i)).not.toBeInTheDocument()

      act(() => {
        vi.advanceTimersByTime(DEMO_PROVISION_SLOW_MS)
      })

      expect(screen.getByText(/taking longer than usual/i)).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('DemoProvisioningDialog accessibility', () => {
  it.each([
    ['provisioning', {}],
    ['failed', { status: 'error' as const, error: new ApiError('boom', 500) }],
    ['timed out', { timedOut: true }],
  ])('has no axe violations while %s', async (_state, props) => {
    renderDialog(props)
    await expectNoAxeViolations(document.body)
  })
})
