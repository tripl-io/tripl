import type { ReactNode } from 'react'
import { Kbd } from '@/components/primitives/kbd'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { commandPaletteShortcutLabel } from '@/lib/platform'
import { GO_TO_SHORTCUTS } from './shell-shortcuts'

/**
 * The `?` sheet: every key the app answers to, in one place. There
 * was no shortcut help at all, so `/` and Ctrl K were found by accident.
 * Loaded on the first `?`, never on a page load.
 */
export default function ShortcutsDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Keyboard shortcuts</DialogTitle>
          <DialogDescription>Letter keys work anywhere outside a text field.</DialogDescription>
        </DialogHeader>
        <DialogBody>
          <dl className="divide-y divide-border-subtle text-body-sm">
            <Shortcut keys={<Kbd>{commandPaletteShortcutLabel()}</Kbd>}>Search or jump to a page</Shortcut>
            <Shortcut keys={<Kbd>/</Kbd>}>Search the list on this page</Shortcut>
            <Shortcut keys={<Kbd>C</Kbd>}>Create on this page (New event, New metric…)</Shortcut>
            <Shortcut keys={<Kbd>?</Kbd>}>Show these shortcuts</Shortcut>
            <Shortcut keys={<Kbd>Esc</Kbd>}>Close a dialog, menu or drawer</Shortcut>
          </dl>
          {/* Two keys, one after the other, within a second; inside a project
              only, since every target is a project page. */}
          <h3 className="mt-4 text-caption font-medium text-fg-secondary">Go to, inside a project</h3>
          <dl className="divide-y divide-border-subtle text-body-sm">
            {GO_TO_SHORTCUTS.map(({ key, label }) => (
              <Shortcut key={key} keys={<Sequence second={key.toUpperCase()} />}>
                {label}
              </Shortcut>
            ))}
          </dl>
        </DialogBody>
      </DialogContent>
    </Dialog>
  )
}

/** `G` then a letter, read as a sequence rather than a chord. */
function Sequence({ second }: { second: string }) {
  return (
    <span className="inline-flex items-center gap-1 text-caption text-fg-secondary">
      <Kbd>G</Kbd>
      then
      <Kbd>{second}</Kbd>
    </span>
  )
}

function Shortcut({ keys, children }: { keys: ReactNode; children: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 py-2">
      <dt className="text-fg">{children}</dt>
      <dd className="shrink-0">{keys}</dd>
    </div>
  )
}
