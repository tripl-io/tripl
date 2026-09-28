import { useRef, useState, type Dispatch, type ReactNode, type SetStateAction } from "react"
import { ChevronRight, History, TriangleAlert } from "lucide-react"

import { MAX_ALERT_RULE_NAME_LENGTH } from "@/api/alerting"
import type { AlertDestination, AlertScopeReadiness, EventType, ScanConfig } from "@/types"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { useDirtySinceOpen, useUnsavedDialogGuard } from "@/hooks/useUnsavedChangesGuard"
import { FieldError } from "@/components/forms/FieldError"
import { REQUIRED_MESSAGE, focusFirstInvalid } from "@/components/forms/validation"

import { channelLabel } from "./channelMeta"
import { fieldErrorId, fieldErrorProps, splitApiFieldErrors } from "./fieldErrors"
import { FilterEditor } from "./FilterEditor"
import { InertScopeNotice } from "./InertScopeNotice"
import { TemplateEditor } from "./TemplateEditor"
import {
  ALL_SCANS_OPTION,
  COOLDOWN_UNITS,
  ITEM_TEMPLATE_VARIABLE_OPTIONS,
  RULE_SIGNAL_GROUPS,
  TEMPLATE_VARIABLE_OPTIONS,
  getDefaultItemsTemplate,
  getDefaultMessageTemplate,
  hasRuleFormProblems,
  isDefaultItemsTemplate,
  isDefaultMessageTemplate,
  joinCooldown,
  ruleDraftSummary,
  ruleFormProblems,
  splitCooldown,
  withMessageFormat,
  type CooldownUnit,
  type RuleFormState,
  type RuleNumericField,
} from "./constants"

/** The payload keys the form has an input (or a group) for. */
const RULE_FORM_FIELDS = [
  'name',
  'cooldown_minutes',
  'min_percent_delta',
  'min_absolute_delta',
  'min_expected_count',
  'message_format',
  'message_template',
  'items_template',
  'filters',
] as const

/** Human names for what a leftover server error may point at. */
const RULE_FIELD_LABELS: Readonly<Record<string, string>> = {
  scan_config_id: 'Scan',
  notify_on_spike: 'Spikes',
  notify_on_drop: 'Drops',
  enabled: 'Rule enabled',
  ai_explanation_enabled: 'AI explanation',
  notify_owners: 'Notify owners',
}

/**
 * The three thresholds, worded as the sentence they complete (AL-2): "Alert
 * when the change is at least 30 %, and at least 0 events". "Min percent
 * delta 100" named the column, not the question, and gave no hint that a
 * drop can never exceed 100%.
 */
const NUMERIC_INPUTS: readonly {
  field: RuleNumericField
  id: string
  label: string
  unit: string
  hint: string
  min: number
  step: string
}[] = [
  {
    field: 'min_percent_delta',
    id: 'rule-min-pct',
    label: 'Alert when the change is at least',
    unit: '%',
    hint: 'A drop can be at most 100% (to zero). 30% is a good start.',
    min: 0,
    step: '1',
  },
  {
    field: 'min_absolute_delta',
    id: 'rule-min-abs',
    label: '…and at least',
    unit: 'events',
    hint: 'Skips small moves on quiet scopes, e.g. 3 → 6.',
    min: 0,
    step: '1',
  },
  {
    field: 'min_expected_count',
    id: 'rule-min-expected',
    label: 'Ignore scopes expecting fewer than',
    unit: 'events',
    hint: 'Scopes this small are too noisy to judge.',
    min: 0,
    step: '1',
  },
]

/** The sighted half of a required field; `aria-required` is the announced half. */
function RequiredMark() {
  return (
    <span aria-hidden="true" className="text-(--danger)">*</span>
  )
}

/** One titled step of the editor, with a one-line lead (AL-1). */
function EditorSection({
  id,
  step,
  title,
  lead,
  children,
}: {
  id: string
  step: number
  title: string
  lead: string
  children: ReactNode
}) {
  return (
    <section aria-labelledby={`${id}-title`} className="grid gap-3 border-t border-border-subtle pt-4 first:border-t-0 first:pt-0">
      <div className="grid gap-0.5">
        <h3 id={`${id}-title`} className="text-body-sm font-semibold text-fg">
          <span className="tnum mr-1.5 text-fg-subtle" aria-hidden="true">{step}</span>
          {title}
        </h3>
        <p className="m-0 text-caption text-fg-subtle">{lead}</p>
      </div>
      {children}
    </section>
  )
}

interface RuleEditorDialogProps {
  open: boolean
  onClose: () => void
  slug: string
  destinations: AlertDestination[]
  /** Which destination the rule routes to. Fixed once the rule exists — see below. */
  destinationId: string
  onDestinationIdChange: (destinationId: string) => void
  /** False while creating; drives the title, the verb, and the picker's lock. */
  isEditing: boolean
  ruleForm: RuleFormState
  setRuleForm: Dispatch<SetStateAction<RuleFormState>>
  eventTypes: EventType[]
  scans: ScanConfig[]
  /** Whether `scans` has answered; until then a bound scan is loading, not blank. */
  scansLoaded?: boolean
  /** The scan list failed to load; a bound scan is unavailable, not loading. */
  scansFailed?: boolean
  /**
   * Replay the rule as SAVED, from inside the editor. Only offered while
   * editing — a rule that does not exist yet has nothing to replay.
   */
  onReplaySaved?: () => void
  /**
   * Replay the rule WITH the edits on this form, unsaved (ALR-12). Offered
   * beside "Replay saved rule" once the form differs from what it opened with.
   */
  onReplayDraft?: () => void
  /**
   * Whether the two drift scopes have any source data in this project.
   * Optional because it rides along on the monitors-summary response: while
   * that request is in flight there is no answer yet, and a form must not
   * accuse a project on the strength of a value it does not have.
   */
  scopeReadiness?: AlertScopeReadiness
  /**
   * Opened as the last step of guided setup (AL-34): the header says "Step 3
   * of 3", so the reader knows the destination they just made is done and
   * this is the part left.
   */
  guidedStep?: boolean
  onSubmit: () => void
  isPending: boolean
  isError: boolean
  error: unknown
}

/**
 * The alert-rule form.
 *
 * It used to live inside `DestinationCard`, which is why it never asked which
 * destination the rule routes to — the card it was rendered in answered that.
 * Now that rules are their own section (tripl-89ps) the question has to be on
 * the form, and it is the first field after the name: a rule with no
 * destination delivers nowhere, and it is the one setting here with no sensible
 * default.
 *
 * The picker is disabled while editing. `updateRule` addresses a rule THROUGH
 * its destination (`/destinations/{id}/rules/{id}`), so re-pointing an existing
 * rule is not an edit the API can express — it would be a delete and a create,
 * silently dropping the rule's delivery history. Offering a control that cannot
 * keep its promise is worse than not offering it.
 */
export function RuleEditorDialog({
  open,
  onClose,
  slug,
  destinations,
  destinationId,
  onDestinationIdChange,
  isEditing,
  ruleForm,
  setRuleForm,
  eventTypes,
  scans,
  scansLoaded = true,
  scansFailed = false,
  onReplaySaved,
  onReplayDraft,
  scopeReadiness,
  guidedStep = false,
  onSubmit,
  isPending,
  isError,
  error,
}: RuleEditorDialogProps) {
  // Drives the format choices, which differ per channel. No fallback: before a
  // destination is picked there is no channel, and offering the FIRST
  // destination's formats showed choices for a channel nobody chose (ALR-4).
  const destination = destinations.find(item => item.id === destinationId) ?? null
  const destinationType = destination?.type ?? null
  // Enabled destinations first; a disabled one is still a valid target (it can
  // be switched back on), but it is not an equal choice (AL-3).
  const destinationOptions = [...destinations].sort(
    (left, right) => Number(right.enabled) - Number(left.enabled),
  )

  // Problems the form can see for itself, named beside their inputs instead of
  // coming back as a raw 422 — or not coming back at all (ALR-5, ALR-14,
  // ALR-15, ALR-16). One timing for all of them (AL-5): a group (scopes,
  // direction) speaks on change, since unticking the last box is the whole
  // mistake; a text field speaks once it is left; everything speaks on submit.
  // A filter row just added is not an error before anyone could fill it.
  const problems = ruleFormProblems(ruleForm)
  const [submitAttempted, setSubmitAttempted] = useState(false)
  const [touched, setTouched] = useState<ReadonlySet<string>>(() => new Set())
  const [messageOpen, setMessageOpen] = useState(false)
  const [openedFor, setOpenedFor] = useState(open)
  if (openedFor !== open) {
    setOpenedFor(open)
    if (open) {
      setSubmitAttempted(false)
      setTouched(new Set())
      // Collapsed unless this rule already carries its own templates: those
      // are the reader's, and hiding them would hide what the rule sends.
      setMessageOpen(
        !isDefaultMessageTemplate(ruleForm.message_template, ruleForm.message_format)
        || !isDefaultItemsTemplate(ruleForm.items_template, ruleForm.message_format),
      )
    }
  }
  const touch = (field: string) =>
    setTouched(current => (current.has(field) ? current : new Set(current).add(field)))
  const shows = (field: string) => submitAttempted || touched.has(field)

  const server = splitApiFieldErrors(isError ? error : null, RULE_FORM_FIELDS, RULE_FIELD_LABELS)
  const numericError = (field: RuleNumericField) =>
    (shows(field) ? problems.numeric[field] : undefined) ?? server.fields[field]
  // The name was the one field left to the browser's `required` bubble, which
  // named only itself and vanished on the next click (AL-28). It is refused
  // here, inline, like every other field of this form.
  const nameProblem = ruleForm.name.trim() ? undefined : REQUIRED_MESSAGE
  const nameError = (shows('name') ? nameProblem : undefined) ?? server.fields.name
  // Create stays enabled without a destination (AL-3): a disabled button
  // 1,300px below the empty picker gave no reason. Submit names it instead.
  const destinationProblem = destinationId ? undefined : 'Pick a destination.'
  const destinationError = submitAttempted ? destinationProblem : undefined
  const blocked =
    hasRuleFormProblems(problems) || nameProblem !== undefined || destinationProblem !== undefined
  const hasShownProblems =
    problems.scopes !== null
    || problems.direction !== null
    || (submitAttempted && blocked)
  const templateServerError =
    server.fields.message_template ?? server.fields.message_format ?? server.fields.items_template
  const formRef = useRef<HTMLFormElement>(null)
  const submit = () => {
    setSubmitAttempted(true)
    if (blocked) {
      // The body scrolls under a fixed header and footer (AL-4), so a refused
      // submit takes the reader to the first field it highlighted.
      requestAnimationFrame(() => {
        if (formRef.current) focusFirstInvalid(formRef.current)
      })
      return
    }
    onSubmit()
  }

  // The cooldown is edited as an amount and a unit (AL-6) and saved as the
  // minutes the API stores. The pair is re-derived whenever it no longer
  // produces the form's value — a reset, a rule opened for editing — and left
  // alone while it does, so typing "1.5" hours is not rewritten under the caret.
  const [cooldownDraft, setCooldownDraft] = useState(() => splitCooldown(ruleForm.cooldown_minutes))
  if (joinCooldown(cooldownDraft.amount, cooldownDraft.unit) !== ruleForm.cooldown_minutes) {
    setCooldownDraft(splitCooldown(ruleForm.cooldown_minutes))
  }
  const setCooldown = (amount: string, unit: CooldownUnit) => {
    setCooldownDraft({ amount, unit })
    setRuleForm(current => ({ ...current, cooldown_minutes: joinCooldown(amount, unit) }))
  }

  // `=== false` rather than `!`: an absent or still-in-flight readiness must
  // render NOTHING. The form would otherwise accuse a project on the strength
  // of a value it does not have yet (tripl-wkwv.1).
  const distributionIsInert =
    ruleForm.include_distribution_drifts && scopeReadiness?.distribution_drift === false
  const valueDriftIsInert =
    ruleForm.include_variable_value_drifts && scopeReadiness?.variable_value_drift === false

  // Metric signals are project-wide, so a scan binding does nothing for a rule
  // that only watches metrics — except stop it from ever firing (JR-15).
  const metricsOnly =
    ruleForm.include_metrics
    && !RULE_SIGNAL_GROUPS.some(group =>
      group.scopes.some(scope => scope.key !== 'include_metrics' && ruleForm[scope.key]),
    )
  const showScanPicker = !metricsOnly || !!ruleForm.scan_config_id

  // The obvious "tell me when X drops" rule at 100% only fires at zero volume.
  // Said, not refused: someone may mean exactly that (AL-2).
  const percent = Number(ruleForm.min_percent_delta.trim())
  const dropNeedsZero =
    ruleForm.notify_on_drop && ruleForm.min_percent_delta.trim() !== '' && percent >= 100

  // Freshness alerts only reach rules that notify on drops, so a spikes-only
  // rule with Source freshness ticked never hears about a late source.
  const freshnessNeedsDrops = ruleForm.include_source_freshness && !ruleForm.notify_on_drop

  const summary = ruleDraftSummary(ruleForm, destination?.name ?? null)

  // Two 8-row templates, filters and thresholds: Escape, a stray overlay click
  // or Cancel used to drop all of it at once (ALR-17). They ask first now,
  // while the form differs from what it opened with.
  const dirty = useDirtySinceOpen(open, { ruleForm, destinationId })
  const unsaved = useUnsavedDialogGuard(dirty)
  const requestClose = () => unsaved.requestClose(onClose)

  const checkbox = (key: keyof RuleFormState & `include_${string}`, label: string, hint: string) => (
    <label key={key} className="flex items-start gap-2 text-body" title={hint}>
      <Checkbox
        className="mt-0.5"
        checked={ruleForm[key] as boolean}
        onCheckedChange={checked => setRuleForm(current => ({ ...current, [key]: !!checked }))}
      />
      <span className="min-w-0">{label}</span>
    </label>
  )

  return (
    <>
    {unsaved.dialog}
    <Dialog open={open} onOpenChange={value => { if (!value) requestClose() }}>
      <DialogContent
        className="max-w-3xl"
        // An open variable-suggestion list takes Escape first: TemplateEditor
        // closes it, and the form stays (ALR-19). Radix listens for Escape on
        // the document before the textarea's own handler runs, so the dialog
        // has to be told here.
        onEscapeKeyDown={event => {
          const target = event.target
          if (
            target instanceof HTMLElement
            && target.getAttribute('role') === 'combobox'
            && target.getAttribute('aria-expanded') === 'true'
          ) {
            event.preventDefault()
          }
        }}
      >
        {/* `noValidate`: every field is checked by the form itself and named
            inline (AL-28). Only the body scrolls; the title and the actions
            stay on screen (AL-4). */}
        <form
          ref={formRef}
          noValidate
          className="flex min-h-0 flex-col gap-4"
          onSubmit={event => { event.preventDefault(); submit() }}
        >
          <DialogHeader className="pr-10 text-left">
            {guidedStep && (
              <p className="m-0 micro-label text-fg-subtle">Step 3 of 3</p>
            )}
            <DialogTitle>{isEditing ? 'Edit alert rule' : 'New alert rule'}</DialogTitle>
          </DialogHeader>
          <DialogBody className="grid gap-5 py-1">
            <div className="grid gap-2">
              <span className="inline-flex items-baseline gap-0.5">
                <Label htmlFor="rule-name">Name</Label>
                <RequiredMark />
              </span>
              <Input
                id="rule-name"
                maxLength={MAX_ALERT_RULE_NAME_LENGTH}
                value={ruleForm.name}
                onChange={event => setRuleForm(current => ({ ...current, name: event.target.value }))}
                onBlur={() => touch('name')}
                aria-required="true"
                {...fieldErrorProps('rule-name', nameError)}
              />
              <FieldError inputId="rule-name" message={nameError} />
            </div>

            {/* 1 · What to watch — the "X" in "tell me when X drops" comes
                first now, with its filters directly under it. They used to
                sit last, under ~900px of template syntax (AL-1). */}
            <EditorSection
              id="rule-what"
              step={1}
              title="What to watch"
              lead="Pick the signals this rule listens to, then narrow them with filters."
            >
              <fieldset
                className="grid gap-3"
                aria-describedby={problems.scopes ? 'rule-scopes-error' : undefined}
              >
                <legend className="sr-only">Signals</legend>
                {/* Two groups rather than one 5-column run (AL-38): volume
                    changes, by the level they are counted at, and the drift
                    detectors, which are a different question. */}
                {RULE_SIGNAL_GROUPS.map(group => (
                  <div key={group.id} className="grid gap-1.5">
                    <p className="m-0 text-body-sm font-medium text-fg">
                      {group.label}
                      <span className="ml-1.5 font-normal text-fg-subtle">{group.hint}</span>
                    </p>
                    <div className="grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-4">
                      {group.scopes.map(scope => checkbox(scope.key, scope.label, scope.hint))}
                    </div>
                  </div>
                ))}
                {problems.scopes && (
                  <p id="rule-scopes-error" className="text-body-sm text-destructive">{problems.scopes}</p>
                )}
              </fieldset>

              {/* The boxes above stay ENABLED while these notices are up. The
                  precondition can be satisfied later, and locking the toggle
                  would make the problem unfixable from the one screen that
                  reports it — the reader would be told what is wrong and denied
                  the switch.

                  `newTab` is set here and on no other caller: the draft around
                  these notices is component state that no exit preserves, so a
                  same-tab link would discard a half-built rule the moment the
                  reader acted on what the notice told them (tripl-wkwv.1). */}
              {(distributionIsInert || valueDriftIsInert) && (
                <div className="grid gap-2">
                  {distributionIsInert && (
                    <InertScopeNotice slug={slug} scope="distribution_drift" newTab />
                  )}
                  {valueDriftIsInert && (
                    <InertScopeNotice slug={slug} scope="variable_value_drift" newTab />
                  )}
                </div>
              )}

              <FilterEditor
                filters={ruleForm.filters}
                eventTypes={eventTypes}
                slug={slug}
                onChange={filters => setRuleForm(current => ({ ...current, filters }))}
                rowErrors={submitAttempted ? problems.filters : {}}
                error={server.fields.filters}
              />
            </EditorSection>

            {/* 2 · When */}
            <EditorSection
              id="rule-when"
              step={2}
              title="When"
              lead="How big a move has to be before anyone hears about it."
            >
              {/* Two independent switches, labelled as such. They read "Up only"
                  and "Down only" while both started ticked — a contradiction —
                  and unticking both saved a rule the API refused (ALR-14). */}
              <fieldset aria-describedby={problems.direction ? 'rule-direction-error' : undefined}>
                <legend className="mb-2 text-body-sm font-medium">Notify on</legend>
                <div className="flex flex-wrap gap-x-6 gap-y-2">
                  <label className="flex items-center gap-2 text-body">
                    <Checkbox
                      checked={ruleForm.notify_on_spike}
                      onCheckedChange={checked => setRuleForm(current => ({ ...current, notify_on_spike: !!checked }))}
                    />
                    Spikes (up)
                  </label>
                  <label className="flex items-center gap-2 text-body">
                    <Checkbox
                      checked={ruleForm.notify_on_drop}
                      onCheckedChange={checked => setRuleForm(current => ({ ...current, notify_on_drop: !!checked }))}
                    />
                    Drops (down)
                  </label>
                </div>
                {problems.direction && (
                  <p id="rule-direction-error" className="mt-2 text-body-sm text-destructive">{problems.direction}</p>
                )}
              </fieldset>
              {freshnessNeedsDrops && (
                <p role="status" className="m-0 inline-flex items-start gap-1.5 text-body-sm text-(--warning)">
                  <TriangleAlert aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
                  Source freshness only alerts on rules that notify on drops. Tick Drops to hear about late or overdue scans.
                </p>
              )}

              <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
                {NUMERIC_INPUTS.map(({ field, id, label, unit, hint, min, step }) => (
                  <div key={field} className="grid content-start gap-1.5">
                    <Label htmlFor={id}>{label}</Label>
                    <div className="flex items-center gap-2">
                      <Input
                        id={id}
                        type="number"
                        inputMode="decimal"
                        min={min}
                        step={step}
                        className="w-28"
                        value={ruleForm[field]}
                        onChange={event => setRuleForm(current => ({ ...current, [field]: event.target.value }))}
                        onBlur={() => touch(field)}
                        {...fieldErrorProps(id, numericError(field))}
                        aria-describedby={numericError(field) ? `${fieldErrorId(id)} ${id}-hint` : `${id}-hint`}
                      />
                      <span className="text-body-sm text-fg-subtle">{unit}</span>
                    </div>
                    <FieldError inputId={id} message={numericError(field)} />
                    <p id={`${id}-hint`} className="m-0 text-caption text-fg-subtle">{hint}</p>
                  </div>
                ))}
              </div>
              {dropNeedsZero && (
                <p role="status" className="m-0 inline-flex items-start gap-1.5 text-body-sm text-(--warning)">
                  <TriangleAlert aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
                  Drops will only alert when volume falls to zero. Lower the percentage to hear about partial drops.
                </p>
              )}

              {/* The text in the box, not `Number(text)`: an emptied field
                  read back as "0" and could not be cleared to retype, and a
                  cleared cooldown saved as 0, which the API refuses (ALR-16).
                  An amount and a unit rather than "1440" minutes (AL-6). */}
              <div className="grid gap-1.5">
                <Label htmlFor="rule-cooldown">Don&apos;t re-alert the same scope for</Label>
                <div className="flex items-center gap-2">
                  <Input
                    id="rule-cooldown"
                    type="number"
                    inputMode="decimal"
                    min={1}
                    step="any"
                    className="w-28"
                    value={cooldownDraft.amount}
                    onChange={event => setCooldown(event.target.value, cooldownDraft.unit)}
                    onBlur={() => touch('cooldown_minutes')}
                    {...fieldErrorProps('rule-cooldown', numericError('cooldown_minutes'))}
                  />
                  <Select
                    value={cooldownDraft.unit}
                    onValueChange={value => setCooldown(cooldownDraft.amount, value as CooldownUnit)}
                  >
                    <SelectTrigger aria-label="Cooldown unit" className="w-32"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {COOLDOWN_UNITS.map(unit => (
                        <SelectItem key={unit.value} value={unit.value}>{unit.value}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <FieldError inputId="rule-cooldown" message={numericError('cooldown_minutes')} />
              </div>
            </EditorSection>

            {/* 3 · Where */}
            <EditorSection
              id="rule-where"
              step={3}
              title="Where"
              lead="The channel that receives the alert."
            >
              <div className="grid gap-2">
                <span className="inline-flex items-baseline gap-0.5">
                  <Label htmlFor="rule-destination">Destination</Label>
                  {!isEditing && <RequiredMark />}
                </span>
                <Select
                  value={destinationId}
                  onValueChange={onDestinationIdChange}
                  disabled={isEditing}
                >
                  <SelectTrigger
                    id="rule-destination"
                    aria-required={!isEditing || undefined}
                    {...fieldErrorProps('rule-destination', destinationError)}
                  >
                    <SelectValue placeholder="Pick a destination" />
                  </SelectTrigger>
                  <SelectContent>
                    {destinationOptions.map(option => (
                      <SelectItem key={option.id} value={option.id}>
                        {/* The separating space sits outside the muted span:
                            inside it, the option's accessible name ran the two
                            together ("Slack· Slack"). */}
                        {option.name}{' '}
                        <span className="text-fg-subtle">
                          {`· ${channelLabel(option.type)}${option.enabled ? '' : ' (disabled)'}`}
                        </span>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <FieldError inputId="rule-destination" message={destinationError} />
                <p className="m-0 text-caption text-fg-subtle">
                  {isEditing
                    ? 'A rule cannot be re-pointed at another destination — that would drop its delivery history. Create a new rule instead.'
                    : 'Where matched signals are delivered.'}
                </p>
                {isEditing && (onReplaySaved || onReplayDraft) && (
                  <div>
                    <div className="flex flex-wrap gap-2">
                      {onReplaySaved && (
                        <Button type="button" variant="outline" size="sm" onClick={onReplaySaved}>
                          <History aria-hidden="true" />
                          Replay saved rule
                        </Button>
                      )}
                      {onReplayDraft && dirty && (
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          onClick={onReplayDraft}
                          // The draft is checked as Save checks it; a form with
                          // problems of its own would only come back refused.
                          disabled={hasRuleFormProblems(problems)}
                        >
                          <History aria-hidden="true" />
                          Replay with these edits
                        </Button>
                      )}
                    </div>
                    <p className="mt-1 text-caption text-fg-subtle">
                      {onReplayDraft && dirty
                        ? hasRuleFormProblems(problems)
                          ? 'Fix the highlighted fields to replay these edits.'
                          : 'Try the edits on this form against past signals before saving them. Nothing is saved.'
                        : 'Replays the rule as it is saved now.'}
                    </p>
                  </div>
                )}
              </div>

              {showScanPicker ? (
                <div className="grid gap-2">
                  <Label htmlFor="rule-scan">Scan</Label>
                  <Select
                    value={ruleForm.scan_config_id || ALL_SCANS_OPTION}
                    onValueChange={value => setRuleForm(current => ({
                      ...current,
                      scan_config_id: value === ALL_SCANS_OPTION ? '' : value,
                    }))}
                  >
                    <SelectTrigger id="rule-scan"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value={ALL_SCANS_OPTION}>All scans</SelectItem>
                      {/* The bound scan's name is not known until the list
                          answers; say so rather than render a blank trigger
                          (ALR-47). */}
                      {!scansLoaded
                        && ruleForm.scan_config_id
                        && !scans.some(scan => scan.id === ruleForm.scan_config_id) && (
                        <SelectItem value={ruleForm.scan_config_id} disabled>
                          {scansFailed ? 'Scan unavailable (scan list failed to load)' : 'Loading scans…'}
                        </SelectItem>
                      )}
                      {scans.map(scan => (
                        <SelectItem key={scan.id} value={scan.id}>
                          {scan.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <p className={`m-0 text-caption ${metricsOnly ? 'text-(--warning)' : 'text-fg-subtle'}`}>
                    {metricsOnly
                      ? 'Metric signals are project-wide, so a rule bound to one scan never delivers them. Pick All scans.'
                      : ruleForm.scan_config_id
                        ? 'Applies to event signals only. Only signals from this scan reach the destination; metric signals are project-wide and are not delivered by a scan-bound rule.'
                        : 'Signals from every scan in the project reach the destination.'}
                  </p>
                </div>
              ) : (
                <p className="m-0 text-caption text-fg-subtle">
                  Metric signals are project-wide, so this rule is not tied to a scan.
                </p>
              )}
            </EditorSection>

            {/* 4 · Message — collapsed by default: the defaults are fine for
                almost everyone, and two raw template editors made a first rule
                look like configuring a mail server (AL-1). */}
            <Collapsible open={messageOpen || !!templateServerError} onOpenChange={setMessageOpen}>
              <section aria-labelledby="rule-message-title" className="grid gap-3 border-t border-border-subtle pt-4">
                <div className="grid gap-0.5">
                  <h3 id="rule-message-title" className="m-0 text-body-sm font-semibold text-fg">
                    <CollapsibleTrigger asChild>
                      <button
                        type="button"
                        className="inline-flex items-center gap-1.5 rounded-sm text-left hover:underline [&[data-state=open]>svg]:rotate-90"
                      >
                        <span className="tnum text-fg-subtle" aria-hidden="true">4</span>
                        Customize message
                        <span className="font-normal text-fg-subtle">(optional)</span>
                        <ChevronRight aria-hidden="true" className="size-3.5 shrink-0 transition-transform" />
                      </button>
                    </CollapsibleTrigger>
                  </h3>
                  <p className="m-0 text-caption text-fg-subtle">
                    The default message lists every matched signal with a link to it.
                  </p>
                </div>
                <CollapsibleContent className="grid gap-5">
                  <TemplateEditor
                    destinationType={destinationType}
                    messageFormat={ruleForm.message_format}
                    onMessageFormatChange={message_format =>
                      setRuleForm(current => withMessageFormat(current, message_format))
                    }
                    title="Message template"
                    variableOptions={TEMPLATE_VARIABLE_OPTIONS}
                    helperText="Type ${var} to get variable suggestions. Use ${items_text} to render the full matched alert list generated from the item template."
                    placeholder={getDefaultMessageTemplate(ruleForm.message_format)}
                    value={ruleForm.message_template}
                    onChange={message_template => setRuleForm(current => ({ ...current, message_template }))}
                    error={server.fields.message_template ?? server.fields.message_format}
                  />

                  <TemplateEditor
                    destinationType={destinationType}
                    messageFormat={ruleForm.message_format}
                    onMessageFormatChange={() => {}}
                    title="Item template"
                    variableOptions={ITEM_TEMPLATE_VARIABLE_OPTIONS}
                    helperText="Rendered for each matched alert item, then joined into ${items_text}. Use ${details_line}, ${monitoring_line} and ${drift_line} for optional context lines."
                    showFormatSelector={false}
                    placeholder={getDefaultItemsTemplate(ruleForm.message_format)}
                    value={ruleForm.items_template}
                    onChange={items_template => setRuleForm(current => ({ ...current, items_template }))}
                    error={server.fields.items_template}
                  />
                </CollapsibleContent>
              </section>
            </Collapsible>

            {/* Advanced: switches most rules never touch, out of the way
                of the four steps above (AL-1). */}
            <section aria-labelledby="rule-advanced-title" className="grid gap-2 border-t border-border-subtle pt-4">
              <h3 id="rule-advanced-title" className="m-0 text-body-sm font-semibold text-fg">Advanced</h3>
              <div className="flex flex-wrap gap-x-6 gap-y-2">
                <label className="flex items-center gap-2 text-body">
                  <Checkbox
                    checked={ruleForm.enabled}
                    onCheckedChange={checked => setRuleForm(current => ({ ...current, enabled: !!checked }))}
                  />
                  Rule enabled
                </label>
                <label className="flex items-center gap-2 text-body">
                  <Checkbox
                    checked={ruleForm.ai_explanation_enabled}
                    onCheckedChange={checked => setRuleForm(current => ({ ...current, ai_explanation_enabled: !!checked }))}
                    aria-describedby="rule-ai-hint"
                  />
                  AI explanation
                </label>
                <label className="flex items-center gap-2 text-body">
                  <Checkbox
                    checked={ruleForm.notify_owners}
                    onCheckedChange={checked => setRuleForm(current => ({ ...current, notify_owners: !!checked }))}
                    aria-describedby="rule-owners-hint"
                  />
                  Also notify owners of the affected event type (by email)
                </label>
              </div>
              <p id="rule-ai-hint" className="m-0 text-caption text-fg-subtle">
                AI explanation appends an LLM summary to each alert (needs AI enabled on the server).
              </p>
              <p id="rule-owners-hint" className="m-0 text-caption text-fg-subtle">
                Owners of each affected event type, and of a catalog metric, get one email per delivery with the items they own, in addition to this destination. Owners who cannot see the project (or have no email) are skipped; needs email (SMTP) configured on the server.
              </p>
            </section>

            {/* One announced line: the per-field messages above are not live
                regions, so a refused submit would otherwise change nothing a
                screen reader says. */}
            {(server.message || (submitAttempted && hasShownProblems) || Object.keys(server.fields).length > 0) && (
              <p role="alert" className="text-body text-destructive">
                {server.message ?? 'Check the highlighted fields.'}
              </p>
            )}
          </DialogBody>
          {/* What Create will set up, in one sentence (AL-1). */}
          {summary && (
            <p className="m-0 shrink-0 text-body-sm text-fg-subtle">
              {summary}
            </p>
          )}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={requestClose}>Cancel</Button>
            <Button type="submit" disabled={isPending}>
              {isEditing ? 'Save' : 'Create'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
    </>
  )
}
