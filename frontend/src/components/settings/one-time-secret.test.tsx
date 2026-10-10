import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { OneTimeSecretDialog, OneTimeSecretField } from './one-time-secret'

const clipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, 'clipboard')

function stubClipboard(value: unknown) {
  Object.defineProperty(navigator, 'clipboard', { value, configurable: true })
}

afterEach(() => {
  if (clipboardDescriptor) Object.defineProperty(navigator, 'clipboard', clipboardDescriptor)
  else Reflect.deleteProperty(navigator, 'clipboard')
})

describe('OneTimeSecretField', () => {
  it('copies with the button, announces it and tells the caller', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    stubClipboard({ writeText })
    const onCopied = vi.fn()
    render(<OneTimeSecretField value="trpl_secret" label="API key" noun="key" onCopied={onCopied} />)

    expect(screen.getByRole('textbox', { name: 'API key' })).toHaveValue('trpl_secret')
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))

    expect(await screen.findByRole('button', { name: 'Copied' })).toBeInTheDocument()
    expect(writeText).toHaveBeenCalledWith('trpl_secret')
    expect(screen.getByRole('status')).toHaveTextContent('API key copied to the clipboard.')
    expect(onCopied).toHaveBeenCalledTimes(1)
  })

  // The no-clipboard path is a manual Ctrl/⌘+C; it counts as a copy too.
  it('counts a copy by hand', () => {
    const onCopied = vi.fn()
    render(<OneTimeSecretField value="https://x/invite/t" label="Invite link" noun="link" onCopied={onCopied} />)

    fireEvent.copy(screen.getByRole('textbox', { name: 'Invite link' }))

    expect(onCopied).toHaveBeenCalledTimes(1)
  })

  it('selects the value and names it when the clipboard is unavailable', async () => {
    stubClipboard(undefined)
    const onCopied = vi.fn()
    render(<OneTimeSecretField value="whsec_1" label="Signing secret" noun="secret" onCopied={onCopied} />)

    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Couldn’t reach the clipboard. The secret above is selected — press Ctrl/⌘+C to copy it.',
    )
    expect(screen.getByRole('textbox', { name: 'Signing secret' })).toHaveFocus()
    expect(onCopied).not.toHaveBeenCalled()
  })
})

describe('OneTimeSecretDialog', () => {
  function renderDialog(secret: string | null, onDone = vi.fn()) {
    const view = render(
      <OneTimeSecretDialog
        secret={secret}
        title="Copy your token now"
        description="This token is shown only once."
        label="Token"
        noun="token"
        details={<p>which token</p>}
        onDone={onDone}
      >
        <p>how to use it</p>
      </OneTimeSecretDialog>,
    )
    return { ...view, onDone }
  }

  it('stays closed without a secret', () => {
    renderDialog(null)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('stays open on Escape and closes only through its footer button', async () => {
    const { onDone } = renderDialog('tok_1')
    const dialog = await screen.findByRole('dialog', { name: 'Copy your token now' })

    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(screen.getByRole('dialog', { name: 'Copy your token now' })).toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: 'Close' })).toBeNull()
    expect(within(dialog).getByText('which token')).toBeInTheDocument()
    expect(within(dialog).getByText('how to use it')).toBeInTheDocument()
    expect(onDone).not.toHaveBeenCalled()

    fireEvent.click(within(dialog).getByRole('button', { name: 'I’ve saved it' }))
    expect(onDone).toHaveBeenCalledTimes(1)
  })

  // The field's "Copied" times out; the footer must not go back to asking.
  it('says Done once the secret was copied, after "Copied" has faded too', async () => {
    stubClipboard({ writeText: vi.fn().mockResolvedValue(undefined) })
    renderDialog('tok_1')
    const dialog = await screen.findByRole('dialog', { name: 'Copy your token now' })

    fireEvent.click(within(dialog).getByRole('button', { name: 'Copy' }))
    expect(await within(dialog).findByRole('button', { name: 'Done' })).toBeInTheDocument()

    await waitFor(
      () => expect(within(dialog).getByRole('button', { name: 'Copy' })).toBeInTheDocument(),
      { timeout: 3000 },
    )
    expect(within(dialog).getByRole('button', { name: 'Done' })).toBeInTheDocument()
  })

  it('starts a new secret out uncopied', async () => {
    const { rerender, onDone } = renderDialog('tok_1')
    const dialog = await screen.findByRole('dialog', { name: 'Copy your token now' })
    fireEvent.copy(within(dialog).getByRole('textbox', { name: 'Token' }))
    expect(within(dialog).getByRole('button', { name: 'Done' })).toBeInTheDocument()

    rerender(
      <OneTimeSecretDialog
        secret="tok_2"
        title="Copy your token now"
        description="This token is shown only once."
        label="Token"
        noun="token"
        onDone={onDone}
      />,
    )

    const next = screen.getByRole('dialog', { name: 'Copy your token now' })
    expect(within(next).getByRole('textbox', { name: 'Token' })).toHaveValue('tok_2')
    expect(within(next).getByRole('button', { name: 'I’ve saved it' })).toBeInTheDocument()
  })
})
