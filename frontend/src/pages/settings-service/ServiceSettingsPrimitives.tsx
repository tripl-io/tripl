import { useId, type ReactNode } from 'react'
import { CheckCircle2, RotateCcw, XCircle } from 'lucide-react'
import type { SettingSource } from '@/types'
import { Chip, type ChipTone, type ChipVariant } from '@/components/primitives/chip'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { SCard, TextInput } from '@/components/settings/kit'
import {
  SECTION_LABELS,
  numberFieldError,
  resetCardDescription,
  type SectionKey,
} from './serviceSettingsHelpers'

/**
 * Small shared bits for the Instance (service) settings sections. The card
 * chrome, fields and controls now come from the redesign kit
 * (`@/components/settings/kit`): each section is composed of titled `SCard`
 * sub-cards with `Field` / `ToggleRow` / `TextInput` / `Select` / `RadioCards`.
 * What stays here is the setting-source badge, the AI connection-test status
 * badge, and the section-level "reset to defaults" card.
 */

const SOURCE_BADGE: Record<
  Exclude<SettingSource, 'default'>,
  { label: string; tone: ChipTone; variant: ChipVariant; title: string }
> = {
  override: {
    label: 'Override',
    tone: 'info',
    variant: 'soft',
    title: 'Stored in this instance’s settings table. A section reset clears it.',
  },
  env: {
    label: 'Env',
    tone: 'neutral',
    variant: 'outline',
    title:
      'Delivered by an environment variable or .env line: the value differs from the built-in default.',
  },
}

/**
 * Where this setting's value came from: a stored override, the environment, or
 * the code default.
 *
 * There used to be no third state, so every field with no stored override was
 * badged "Env" — including ones nothing had ever delivered, which is how an
 * instance could assert it had been told where to send embeddings when it had
 * not (tripl-wkwv.2).
 *
 * A value at its built-in default carries no badge at all: on a fresh instance
 * every one of ~40 rows wore a grey "Default" pill, which said nothing and hid
 * the rare rows that matter (ST-25). The page's legend says what an unbadged
 * row means, including that "at the default" and "delivered, but equal to the
 * default" cannot be told apart from here.
 */
export function SourceBadge({ source }: { source: SettingSource }) {
  if (source === 'default') return null
  const { label, tone, variant, title } = SOURCE_BADGE[source]
  // The badge taxonomy's pill (DS-6): the size comes from `size`.
  return (
    <Chip tone={tone} variant={variant} size="xs" title={title}>
      {label}
    </Chip>
  )
}

/**
 * Pass/fail result chip for the AI and email connection checks. A failure is
 * the danger tone, not helper-text grey: a failed test is the reason the card
 * exists, and it used to read weaker than a pass.
 */
export function StatusBadge({ active, label }: { active: boolean; label: string }) {
  return (
    <span
      className={
        active
          ? 'inline-flex items-center gap-1 text-body-sm text-success'
          : 'inline-flex items-center gap-1 text-body-sm text-destructive'
      }
    >
      {active ? <CheckCircle2 className="h-3 w-3" aria-hidden="true" /> : <XCircle className="h-3 w-3" aria-hidden="true" />}
      {label}
    </span>
  )
}

/**
 * The rows that depend on a master switch, de-emphasised while it is off
 * (ST-26). They stay editable — preparing a config before switching it on is
 * valid — but with every field looking live, "Test AI" and the HSTS max age
 * read as working while their switch said otherwise. `data-inactive` lets a
 * test (or a style) find the state without reading opacity.
 */
export function InactiveGroup({
  inactive,
  reason,
  children,
}: {
  inactive: boolean
  /** "Not used while <switch> is off." Omit when the card already says so. */
  reason?: string
  children: ReactNode
}) {
  if (!inactive) return <>{children}</>
  return (
    <div data-inactive="true">
      {reason && (
        <p className="m-0 px-4 pt-2.5 text-caption text-fg-tertiary">
          {reason}
        </p>
      )}
      <div className="opacity-60 transition-opacity focus-within:opacity-100 hover:opacity-100">
        {children}
      </div>
    </div>
  )
}

/** Why an operator field cannot be changed from an organization admin's session. */
export const OPERATOR_ONLY_REASON =
  'Only a platform admin can change these: they apply to every organization on this instance.'

/**
 * Operator-only fields (backend `OPERATOR_FIELDS`) inside a section an
 * organization admin may open. For anyone but a platform admin they are shown
 * and disabled, with the reason once: a save carrying one is refused with 403,
 * which also threw away the organization fields saved beside it.
 */
export function OperatorFields({
  locked,
  quiet = false,
  children,
}: {
  locked: boolean
  /** Leave the reason out when an earlier group in the same card said it. */
  quiet?: boolean
  children: ReactNode
}) {
  if (!locked) return <>{children}</>
  return (
    <fieldset disabled data-operator-only="true" className="m-0 min-w-0 border-0 p-0">
      {!quiet && (
        <p className="m-0 px-4 pt-2.5 text-caption text-fg-tertiary">{OPERATOR_ONLY_REASON}</p>
      )}
      <div className="opacity-60">{children}</div>
    </fieldset>
  )
}

/** Placeholder cards while the instance settings (or their chunk) load. */
export function InstanceSettingsSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading instance settings">
      {[3, 2].map((rows, card) => (
        <SCard key={card}>
          <div className="px-4 py-4">
            <Skeleton className="h-3.5 w-32" />
          </div>
          {Array.from({ length: rows }, (_, row) => (
            // Same 560px container step as the FormRow it stands in for, so the
            // layout does not jump when the data lands.
            <div key={row} className="@container">
              <div
                className="flex flex-col gap-2 px-4 py-[15px] @min-[560px]:flex-row @min-[560px]:items-center @min-[560px]:gap-6 border-t border-t-border-subtle"
              >
                <Skeleton className="h-3 w-28 shrink-0" />
                <Skeleton className="h-[34px] w-full" />
              </div>
            </div>
          ))}
        </SCard>
      ))}
    </div>
  )
}

/**
 * A numeric setting's input. The text is handed to `setField` as typed, so an
 * emptied field stays empty instead of snapping to 0, and a value the backend
 * would refuse is named under the input (and blocks Save) instead of coming
 * back as a 422.
 */
export function NumberSettingInput({
  section,
  field,
  value,
  saved,
  setField,
  suffix,
}: {
  section: SectionKey
  field: string
  value: number | string
  /** The stored value; left as it is, it is not this form's to reject. */
  saved: number
  setField: (section: SectionKey, field: string, value: string | number | boolean) => void
  suffix?: string
}) {
  const errorId = useId()
  const error = value === saved ? null : numberFieldError(section, field, value)
  return (
    <>
      <TextInput
        type="number"
        value={String(value)}
        onChange={next => setField(section, field, next)}
        suffix={suffix}
        mono
        aria-invalid={error !== null}
        aria-describedby={error ? errorId : undefined}
      />
      {error && (
        <p id={errorId} className="mt-1 text-caption text-danger">
          {error}
        </p>
      )}
    </>
  )
}

/**
 * Section-level "reset to defaults" card, rendered once, last, below every
 * sub-card it can null.
 *
 * It used to be a neutral footer on the section's FIRST sub-card while it reset
 * the whole section: on Security & access it sat in "Sessions" (3 fields) and
 * reverted all 13 security overrides, including Registration in the card above
 * it and the CSP and rate limits in the cards below (tripl-ifiy). The count and
 * the danger tone here state the blast radius the position used to hide.
 *
 * `overrides` is what makes that count honest: it is the number of fields the
 * badges above are calling "Override" right now, not the number of fields a
 * reset is able to null.
 */
export function ResetSectionCard({
  section,
  overrides,
  onReset,
  resetting,
}: {
  section: SectionKey
  /** Fields in this section currently badged "Override" (see overrideCount). */
  overrides: number
  onReset: () => void
  resetting: boolean
}) {
  const label = SECTION_LABELS[section]
  // Nothing stored, nothing to clear: no card. On a fresh instance a full card
  // holding a disabled button ended all six pages, as tall as Runtime's
  // settings themselves (ST-29); red with a live button before that, it taught
  // people to ignore the one colour kept for real consequences (tripl-5qp9).
  if (overrides === 0) return null
  return (
    <SCard
      tone="danger"
      title={`Reset ${label} to defaults`}
      description={resetCardDescription(section, overrides)}
      footer={
        <>
          <Button type="button" variant="outline" size="sm" onClick={onReset} disabled={resetting}>
            <RotateCcw className="h-3.5 w-3.5" />
            Reset to defaults
          </Button>
          <span className="text-caption text-fg-tertiary">
            Applies immediately — it does not wait for Save changes.
          </span>
        </>
      }
    />
  )
}
