import { useState } from "react"

import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"

import {
  CADENCE_OPTIONS,
  WEEKDAY_LABELS,
  cadenceToCron,
  cronToCadence,
  describeCron,
  formatInProjectZone,
  validateCadence,
  type CadenceDraft,
  type CadenceMode,
} from "./deliverySchedule"

interface DeliveryScheduleFieldProps {
  /** The cron expression currently on the form, '' meaning immediate. */
  value: string
  onChange: (cron: string) => void
  /**
   * Whether what is ON SCREEN can be saved. The field only publishes a valid
   * cadence through `onChange`, so while the draft is invalid the form still
   * holds the last good expression — and saving then stored a schedule other
   * than the one shown, under a visible error. The dialog listens here
   * and refuses the submit until the draft validates again.
   */
  onValidityChange?: (valid: boolean) => void
  /** The project's IANA zone, so the copy can say which clock these times are on. */
  projectTimezone: string
  /** When the next digest is due, from the server. Null while immediate. */
  nextDigestAt?: string | null
  /**
   * The server's rejection of the saved cadence (a 422 on
   * `delivery_schedule_cron`), for a cron the client check let through — "0 25
   * * * *" has five fields but no hour 25. Rendered in the same message slot as
   * the client error so the inputs point at it; the client error wins while
   * the draft on screen is itself invalid.
   */
  serverError?: string | null
  disabled?: boolean
}

/**
 * Pick how often a destination delivers.
 *
 * The presets and the cron box edit the same underlying string — the form
 * holds a cron expression and nothing else — so switching to Custom shows what
 * the preset was actually asking for rather than starting from blank.
 */
export function DeliveryScheduleField({
  value,
  onChange,
  onValidityChange,
  projectTimezone,
  nextDigestAt,
  serverError = null,
  disabled = false,
}: DeliveryScheduleFieldProps) {
  // The draft is held locally rather than derived from `value` on every
  // render. Deriving it looks tidier and is wrong: a half-typed time is not a
  // valid cron, `cadenceToCron` returns null for it, and the field would
  // silently reset the destination to "immediate" while the operator was still
  // typing — turning a typo into a paging change nobody asked for.
  const [draft, setDraft] = useState<CadenceDraft>(() => cronToCadence(value))

  // Re-seed when `value` changes from OUTSIDE (opening a different
  // destination), but never while the local draft still means the same thing —
  // that would clobber whatever is being typed. Adjusted during render rather
  // than in an effect, which is React's documented shape for "reset state when
  // a prop changes" and the shape ProjectGeneralSection already uses to
  // hydrate its form from a query.
  const [seededFrom, setSeededFrom] = useState(value)
  if (seededFrom !== value) {
    setSeededFrom(value)
    if ((cadenceToCron(draft) ?? "") !== value) setDraft(cronToCadence(value))
  }

  const error = validateCadence(draft) ?? (serverError || null)

  const update = (next: CadenceDraft) => {
    setDraft(next)
    // Only a valid cadence is published upwards. An invalid one keeps the last
    // good expression on the form and shows the error below, so the schedule
    // never changes to something the operator did not choose — and the owner
    // is told, so it cannot save that last good expression as if it were what
    // is on screen.
    const valid = validateCadence(next) === null
    if (valid) onChange(cadenceToCron(next) ?? "")
    onValidityChange?.(valid)
  }

  return (
    <div className="grid gap-2">
      <Label htmlFor="destination-cadence">Delivery schedule</Label>
      <Select
        value={draft.mode}
        onValueChange={mode => {
          const next = { ...draft, mode: mode as CadenceMode }
          // Seed Custom from whatever the preset currently means, so the box is
          // never blank and an accidental switch loses nothing.
          if (mode === "custom") next.cron = cadenceToCron(draft) ?? ""
          update(next)
        }}
        disabled={disabled}
      >
        <SelectTrigger
          id="destination-cadence"
          // Only the mode picker exists for "immediate" and the presets, so it
          // carries the error there; with a time or cron box the box does.
          aria-invalid={error && !hasCadenceInput(draft.mode) ? true : undefined}
          aria-describedby={error && !hasCadenceInput(draft.mode) ? "destination-cadence-error" : undefined}
        >
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {CADENCE_OPTIONS.map(option => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      {(draft.mode === "daily" || draft.mode === "weekly") && (
        <div className="grid gap-2 sm:grid-cols-2">
          {draft.mode === "weekly" && (
            <Select
              value={String(draft.weekday)}
              onValueChange={weekday => update({ ...draft, weekday: Number(weekday) })}
              disabled={disabled}
            >
              <SelectTrigger id="destination-cadence-weekday" aria-label="Day of week">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {WEEKDAY_LABELS.map((label, index) => (
                  <SelectItem key={label} value={String(index)}>
                    {label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          {/* The native time picker, not free text: a phone gets a
              time wheel instead of a full keyboard, and "9:30am" or "09.30"
              cannot be typed in the first place. It always yields "HH:MM",
              the shape `validateCadence` reads. The multi-time box below
              stays text — a list of times is not one time. */}
          <Input
            id="destination-cadence-time"
            type="time"
            aria-label="Time of day"
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? "destination-cadence-error" : undefined}
            value={draft.time}
            onChange={event => update({ ...draft, time: event.target.value })}
            disabled={disabled}
          />
        </div>
      )}

      {draft.mode === "times_of_day" && (
        <Input
          id="destination-cadence-times"
          aria-label="Times of day"
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? "destination-cadence-error" : undefined}
          placeholder="09:00, 18:00"
          value={draft.times}
          onChange={event => update({ ...draft, times: event.target.value })}
          disabled={disabled}
        />
      )}

      {draft.mode === "custom" && (
        <Input
          id="destination-cadence-cron"
          aria-label="Cron expression"
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? "destination-cadence-error" : undefined}
          placeholder="0 9,18 * * 1-5"
          value={draft.cron}
          onChange={event => update({ ...draft, cron: event.target.value })}
          disabled={disabled}
        />
      )}

      {error ? (
        <p id="destination-cadence-error" className="text-body-sm text-destructive">{error}</p>
      ) : (
        <p className="text-body-sm text-fg-tertiary">
          {draft.mode === "immediate"
            ? "Alerts are sent as soon as a collection finds something."
            : `${describeCron(value)} (${projectTimezone}). Everything found in between is collected into one message.`}
          {nextDigestAt && draft.mode !== "immediate"
            ? ` Next: ${formatInProjectZone(nextDigestAt, projectTimezone)}.`
            : ""}
        </p>
      )}
    </div>
  )
}

/** Whether the mode shows a text or time box of its own beside the picker. */
function hasCadenceInput(mode: CadenceMode): boolean {
  return mode === "daily" || mode === "weekly" || mode === "times_of_day" || mode === "custom"
}
