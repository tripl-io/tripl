import { useRef } from 'react'
import { Copy } from 'lucide-react'
import { SCard } from '@/components/settings/kit'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { useCopyToClipboard } from '@/hooks/useCopyToClipboard'

/**
 * The webhook's signing secret, the one time it is shown (F20): after the save
 * that created the webhook and after a rotation. The server keeps it encrypted
 * and never returns it again, so leaving this card is the last chance to copy
 * it. A failed copy selects the text for a manual Ctrl/⌘+C.
 */
export function AuditWebhookSecretReveal({ secret, onDone }: { secret: string; onDone: () => void }) {
  const inputRef = useRef<HTMLInputElement>(null)
  const { state, copy } = useCopyToClipboard(inputRef)
  return (
    <SCard
      title="Copy the signing secret now"
      description="It is shown only this once. Your receiver uses it to verify the X-Tripl-Signature header of every delivery."
    >
      <div className="flex flex-wrap items-center gap-2 px-4 py-3">
        <Input
          ref={inputRef}
          readOnly
          value={secret}
          aria-label="Signing secret"
          className="mono min-w-0 flex-1"
          onFocus={(event) => event.currentTarget.select()}
        />
        <Button type="button" variant="outline" onClick={() => void copy(secret)}>
          <Copy aria-hidden="true" />
          {state === 'copied' ? 'Copied' : 'Copy'}
        </Button>
        <Button type="button" onClick={onDone}>
          {state === 'copied' ? 'Done' : 'I’ve saved it'}
        </Button>
      </div>
      {state === 'failed' && (
        <p role="alert" className="m-0 px-4 pb-3 text-body-sm text-danger">
          Couldn’t reach the clipboard. The secret above is selected — press Ctrl/⌘+C to copy it.
        </p>
      )}
      {state === 'copied' && (
        <p role="status" className="m-0 px-4 pb-3 text-body-sm text-fg-muted">
          Copied to the clipboard.
        </p>
      )}
    </SCard>
  )
}
