import { useId } from 'react'
import { EvTextarea, SurfCard } from './eventFormLayout'

export interface DraftDiscussionNoteProps {
  value: string
  onChange: (next: string) => void
}

/**
 * The Discussion composer on the CREATE form.
 *
 * A comment needs an event to hang on, so the thread itself cannot exist yet —
 * but the question does. "Should this fire on cancel too?" occurs while the
 * event is being authored, and by the time it exists its author has navigated
 * away.
 *
 * It is not Description and not Title, and the copy says so: everything else on
 * this page travels with the event into the spec the implementer reads, while
 * this is the one box that does not.
 *
 * The form's own card and textarea (AU-8): it used to be a `rounded-md p-3`
 * box with a bigger textarea and no header rule, which read as a widget pasted
 * below the form rather than one more section of it.
 */
export function DraftDiscussionNote({ value, onChange }: DraftDiscussionNoteProps) {
  const id = useId()
  return (
    <SurfCard
      title="Discussion"
      subtitle="Kept out of the spec: nothing here travels with the event to whoever implements it."
    >
      <div className="px-4 py-3">
        <label htmlFor={id} className="sr-only">
          A question or note about this event, posted as the first comment the moment the event is
          created
        </label>
        <EvTextarea
          id={id}
          rows={2}
          value={value}
          onChange={event => onChange(event.target.value)}
          placeholder="Should this fire on cancel too?"
        />
        <p className="mt-1 text-caption text-fg-tertiary">
          Posted as the first comment the moment the event is created.
        </p>
      </div>
    </SurfCard>
  )
}
