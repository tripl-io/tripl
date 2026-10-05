import { useRef, useState, type ComponentProps } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Eye, EyeOff, Send } from 'lucide-react'

import { alertingApi } from '@/api/alerting'
import { Button } from '@/components/ui/button'
import { IconButton } from '@/components/ui/icon-button'
import { Checkbox } from '@/components/ui/checkbox'
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { NativeSelect } from '@/components/settings/kit'
import { useDirtySinceOpen, useUnsavedDialogGuard } from '@/hooks/useUnsavedChangesGuard'
import { FieldError } from '@/components/forms/FieldError'
import { examplePlaceholder } from '@/components/forms/placeholders'
import { REQUIRED_MESSAGE, focusFirstInvalid, missingSummary } from '@/components/forms/validation'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import type { AlertDestination } from '@/types'

import { invalidateAlertingConfig } from './alertingCache'
import { ChannelGlyph, channelLabel } from './channelMeta'
import {
  PAGERDUTY_SEVERITIES,
  defaultDestinationForm,
  type DestinationChannel,
  type DestinationFormState,
} from './constants'
import { DeliveryScheduleField } from './DeliveryScheduleField'
import { resolveScheduleTimezone } from './deliverySchedule'
import {
  DESTINATION_FIELD_LABELS,
  DESTINATION_FIELD_MAX_LENGTH,
  destinationFormProblems,
  destinationFormToPayload,
  destinationFormToTestBody,
  destinationToForm,
} from './destinationForm'
import { DestinationTestResult } from './DestinationTestResult'
import { attachDestinationServerErrors } from './destinationServerErrors'
import { fieldErrorProps, splitApiFieldErrors } from './fieldErrors'

/** What the dialog was opened for. */
export type DestinationDialogTarget =
  | {
      mode: 'create'
      type: DestinationChannel
      /**
       * Hand the new destination straight on to a rule form — guided setup's
       * step 2 → 3. Decided by whoever OPENED the dialog, not on success: an
       * editor adding a second channel from Destinations is not in a setup
       * flow and must stay where they are.
       */
      handOffToRule: boolean
    }
  | { mode: 'edit'; destination: AlertDestination }

interface DestinationDialogProps {
  slug: string
  target: DestinationDialogTarget
  /** Read for its timezone, so the schedule copy names the clock it runs on. */
  project: { timezone?: string } | undefined
  isDemo: boolean
  onClose: () => void
  onCreated: (created: AlertDestination, handOffToRule: boolean) => void
}

// A password manager treats "a text field, then a password field" as a login
// form: it offers to autofill the operator's saved tripl password into a webhook
// or token box, and to "save a password" under the destination's name.
// `new-password` is the one autocomplete value browsers honour for "not mine",
// and the two data attributes are 1Password's and LastPass's own opt-outs.
const SECRET_INPUT_PROPS = {
  autoComplete: 'new-password',
  'data-1p-ignore': true,
  'data-lpignore': 'true',
} as const

/**
 * A write-only credential: masked, with a toggle to check what was typed.
 *
 * The webhook Target URL is stored encrypted exactly like a token
 * (`target_url_set`), but it was a plain text box echoing the secret on screen
 * while being typed.
 */
function SecretInput({ label, ...props }: ComponentProps<typeof Input> & { label: string }) {
  const [shown, setShown] = useState(false)
  return (
    <div className="flex gap-1">
      <Input {...props} {...SECRET_INPUT_PROPS} type={shown ? 'text' : 'password'} />
      <IconButton
        type="button"
        variant="ghost"
        className="shrink-0"
        label={`${shown ? 'Hide' : 'Show'} ${label}`}
        aria-pressed={shown}
        onClick={() => setShown(current => !current)}
      >
        {shown ? <EyeOff aria-hidden="true" className="h-4 w-4" /> : <Eye aria-hidden="true" className="h-4 w-4" />}
      </IconButton>
    </div>
  )
}

/** PagerDuty's four severities, as the select names them. */
const PAGERDUTY_SEVERITY_OPTIONS = PAGERDUTY_SEVERITIES.map(value => ({
  value,
  label: value.charAt(0).toUpperCase() + value.slice(1),
}))

/** The inputs each channel renders — what a server error may be attached to. */
const CHANNEL_FIELDS: Record<DestinationFormState['type'], readonly (keyof DestinationFormState)[]> = {
  slack: ['webhook_url'],
  telegram: ['bot_token', 'chat_id'],
  webhook: ['target_url', 'webhook_header_name', 'webhook_header_value'],
  email: ['email_recipients', 'email_from_address', 'email_subject_template'],
  jira: ['jira_base_url', 'jira_auth_email', 'jira_api_token', 'jira_project_key', 'jira_issue_type'],
  linear: ['linear_api_key', 'linear_team_id', 'linear_state_id', 'linear_label_ids'],
  pagerduty: ['pagerduty_routing_key', 'pagerduty_severity'],
  teams: ['teams_webhook_url'],
  demo_sink: [],
}

/**
 * The destination create/edit form, lifted out of the page.
 *
 * It lived inline in ProjectAlertingTab beside the inbox logic, with create and
 * update building their bodies forty lines apart and disagreeing about which
 * fields went on the wire — which is how create came to send `chat_id: ''` for
 * every channel. Both now go through `destinationFormToPayload`.
 *
 * Mounted per opening (the page keys it), so a new opening starts with a fresh
 * form AND fresh mutations: a failed create followed by Cancel no longer
 * reopens on the next channel with the old error already showing.
 */
export function DestinationDialog({
  slug,
  target,
  project,
  isDemo,
  onClose,
  onCreated,
}: DestinationDialogProps) {
  const qc = useQueryClient()
  const existing = target.mode === 'edit' ? target.destination : null
  const [form, setForm] = useState<DestinationFormState>(() =>
    existing ? destinationToForm(existing) : defaultDestinationForm(target.mode === 'create' ? target.type : 'slack'),
  )
  const [removeWebhookHeader, setRemoveWebhookHeader] = useState(false)
  const [scheduleValid, setScheduleValid] = useState(true)
  // Client-side problems are named once a submit has been tried, not while the
  // form is being filled in: a pair that is half-typed is not yet a mistake.
  const [submitAttempted, setSubmitAttempted] = useState(false)

  const createMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () => alertingApi.createDestination(slug, destinationFormToPayload(form, null)),
    onSuccess: created => {
      invalidateAlertingConfig(qc, slug)
      onCreated(created, target.mode === 'create' && target.handOffToRule)
    },
  })
  const updateMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (destination: AlertDestination) =>
      alertingApi.updateDestination(
        slug,
        destination.id,
        destinationFormToPayload(form, destination, { removeWebhookHeader }),
      ),
    onSuccess: () => {
      invalidateAlertingConfig(qc, slug)
      onClose()
    },
  })
  const mutation = existing ? updateMut : createMut

  // "Send test" before saving: setting up Slack used to take Create,
  // close, find the card, then Test. The result describes the settings it was
  // sent with, so it is shown only while the form still holds exactly those —
  // `setForm` makes a new object on every edit, so identity is the check.
  const [testedWith, setTestedWith] = useState<{
    form: DestinationFormState
    removeWebhookHeader: boolean
  } | null>(null)
  const testMut = useMutation({
    // Its outcome renders inline, transport failure included.
    meta: SILENT_ERROR_META,
    mutationFn: (sent: { form: DestinationFormState; removeWebhookHeader: boolean }) =>
      alertingApi.testDestinationDraft(
        slug,
        destinationFormToTestBody(sent.form, existing, { removeWebhookHeader: sent.removeWebhookHeader }),
      ),
  })
  const sendTest = () => {
    const sent = { form, removeWebhookHeader }
    setTestedWith(sent)
    testMut.mutate(sent)
  }
  const testIsCurrent =
    testedWith !== null
    && testedWith.form === form
    && testedWith.removeWebhookHeader === removeWebhookHeader

  // Same as the rule dialog: a close that would drop typed-in
  // credentials or templates asks first.
  const guard = useUnsavedDialogGuard(useDirtySinceOpen(true, { form, removeWebhookHeader }))
  const requestClose = () => guard.requestClose(onClose)

  const problems = destinationFormProblems(form, existing, { removeWebhookHeader })
  const serverKnownFields = ['name', 'delivery_schedule_cron', ...CHANNEL_FIELDS[form.type]] as const
  // In the form's words and under the input they are about, including the
  // plain-string refusals (the SSRF guard) that name a field.
  const server = attachDestinationServerErrors(
    splitApiFieldErrors(mutation.error, serverKnownFields, DESTINATION_FIELD_LABELS),
    serverKnownFields,
  )
  // Secrets are required where nothing is stored yet — except on a demo
  // workspace, whose disabled Slack example has no webhook and must stay
  // renameable.
  const secretRequired = (isSet: boolean | undefined) => !existing || (!isSet && !isDemo)
  // What the form refuses to send empty, per channel. The fields that are
  // pre-filled and not secret stay required in edit mode too: an emptied Base
  // URL used to be sent as "absent", so Save "succeeded" and quietly kept the
  // old value. Checked here and named inline, not by the browser's
  // `required` bubble, which flagged the first empty field only.
  const requiredFields: readonly (keyof DestinationFormState)[] = [
    'name',
    ...({
      slack: secretRequired(existing?.webhook_set) ? ['webhook_url' as const] : [],
      telegram: [
        ...(secretRequired(existing?.bot_token_set) ? ['bot_token' as const] : []),
        'chat_id' as const,
      ],
      webhook: secretRequired(existing?.target_url_set) ? ['target_url' as const] : [],
      email: ['email_recipients' as const],
      jira: [
        'jira_base_url' as const,
        'jira_auth_email' as const,
        ...(secretRequired(existing?.jira_api_token_set) ? ['jira_api_token' as const] : []),
        'jira_project_key' as const,
      ],
      linear: [
        ...(secretRequired(existing?.linear_api_key_set) ? ['linear_api_key' as const] : []),
        'linear_team_id' as const,
      ],
      pagerduty: secretRequired(existing?.pagerduty_routing_key_set) ? ['pagerduty_routing_key' as const] : [],
      teams: secretRequired(existing?.teams_webhook_set) ? ['teams_webhook_url' as const] : [],
      demo_sink: [],
    } satisfies Record<DestinationFormState['type'], (keyof DestinationFormState)[]>)[form.type],
  ]
  const missing = requiredFields.filter(field => !String(form[field] ?? '').trim())
  const errorFor = (field: keyof DestinationFormState) =>
    (submitAttempted ? problems[field] ?? (missing.includes(field) ? REQUIRED_MESSAGE : undefined) : undefined)
    ?? server.fields[field]

  const formRef = useRef<HTMLFormElement>(null)
  const submit = () => {
    setSubmitAttempted(true)
    if (!scheduleValid || missing.length > 0 || Object.keys(problems).length > 0) {
      // Only the body scrolls: take the reader to the first field the
      // refusal highlighted instead of leaving it below the fold.
      requestAnimationFrame(() => {
        if (formRef.current) focusFirstInvalid(formRef.current)
      })
      return
    }
    if (existing) updateMut.mutate(existing)
    else createMut.mutate()
  }
  const set = <K extends keyof DestinationFormState>(field: K, value: DestinationFormState[K]) =>
    setForm(current => ({ ...current, [field]: value }))

  /** One text input with its label, limits and error, by payload field. */
  const textField = (
    field: keyof typeof DESTINATION_FIELD_MAX_LENGTH | 'email_recipients',
    id: string,
    label: string,
    { optional = false, ...extra }: Partial<ComponentProps<typeof Input>> & { optional?: boolean } = {},
  ) => {
    const error = errorFor(field)
    return (
      <div className="grid gap-2">
        <Label htmlFor={id} optional={optional}>{label}</Label>
        <Input
          id={id}
          autoComplete="off"
          maxLength={field === 'email_recipients' ? undefined : DESTINATION_FIELD_MAX_LENGTH[field]}
          value={form[field]}
          onChange={event => set(field, event.target.value)}
          aria-required={requiredFields.includes(field) || undefined}
          {...fieldErrorProps(id, error)}
          {...extra}
        />
        <FieldError inputId={id} message={error} />
      </div>
    )
  }

  /** One write-only credential, by payload field. */
  const secretField = (
    field:
      | 'webhook_url'
      | 'bot_token'
      | 'target_url'
      | 'webhook_header_value'
      | 'jira_api_token'
      | 'linear_api_key'
      | 'pagerduty_routing_key'
      | 'teams_webhook_url',
    id: string,
    label: string,
    { placeholder, optional = false }: { placeholder: string; optional?: boolean },
  ) => {
    const error = errorFor(field)
    return (
      <div className="grid gap-2">
        <Label htmlFor={id} optional={optional}>{label}</Label>
        <SecretInput
          id={id}
          label={label}
          placeholder={placeholder}
          value={form[field]}
          onChange={event => set(field, event.target.value)}
          aria-required={requiredFields.includes(field) || undefined}
          {...fieldErrorProps(id, error)}
        />
        <FieldError inputId={id} message={error} />
      </div>
    )
  }

  // One announced line for the whole form. The per-input messages are not live
  // regions, so without this a rejected submit would change nothing a screen
  // reader says.
  const hasFieldErrors = [...CHANNEL_FIELDS[form.type], 'name' as const, 'delivery_schedule_cron' as const]
    .some(field => !!errorFor(field))
  const alertMessage =
    submitAttempted && !scheduleValid
      ? 'Fix the delivery schedule above before saving — the cadence on screen is not valid yet.'
      : server.message
        ?? (submitAttempted ? missingSummary(missing.map(field => DESTINATION_FIELD_LABELS[field] ?? field)) : null)
        ?? (hasFieldErrors ? 'Check the highlighted fields.' : null)

  return (
    <>
      {guard.dialog}
      <Dialog open onOpenChange={open => { if (!open) requestClose() }}>
        <DialogContent className="max-w-lg">
          {/* `noValidate`: required fields are named inline and in the line
              above the actions, not by a browser bubble. Only the body
              scrolls; the title and the actions stay on screen. */}
          <form
            ref={formRef}
            noValidate
            className="flex min-h-0 flex-col gap-4"
            onSubmit={event => { event.preventDefault(); submit() }}
          >
            <DialogHeader>
              {/* The channel's icon beside the title, so the choice made on the
                  button that opened this reads as made. */}
              <DialogTitle className="flex items-center gap-2">
                <ChannelGlyph type={form.type} aria-hidden="true" className="size-4 shrink-0 text-fg-tertiary" />
                {existing ? 'Edit destination' : `New ${channelLabel(form.type)} destination`}
              </DialogTitle>
            </DialogHeader>
            <DialogBody className="grid gap-4 py-1">
              {/* No channel select. On create the button that opened
                  this already chose the channel and the title says so; a
                  select here only offered to wipe the form. A channel is fixed
                  once saved, so edit shows it as a read-only line. */}
              {existing ? (
                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                  {textField('name', 'dest-name', 'Name')}
                  <div className="grid gap-2">
                    <span className="text-body leading-none font-medium">Channel</span>
                    <p className="flex h-9 items-center gap-2 text-body text-fg-tertiary" data-testid="dest-channel">
                      <ChannelGlyph type={form.type} aria-hidden="true" className="size-4 shrink-0" />
                      {channelLabel(form.type)}
                    </p>
                  </div>
                </div>
              ) : (
                textField('name', 'dest-name', 'Name')
              )}

              {form.type === 'slack' && (
                <div className="grid gap-2">
                  {secretField('webhook_url', 'dest-webhook-url', 'Webhook URL', {
                    placeholder: existing?.webhook_set ? 'Leave empty to keep current webhook' : examplePlaceholder('https://hooks.slack.com/...'),
                  })}
                  {/* Where the URL comes from, which the form never said. */}
                  <p className="text-body-sm text-fg-tertiary">
                    Create an Incoming Webhook in Slack (Apps → Incoming Webhooks),
                    pick the channel, and paste its URL here.{' '}
                    <a
                      href="https://api.slack.com/messaging/webhooks"
                      target="_blank"
                      rel="noreferrer"
                      className="underline underline-offset-2 hover:text-foreground"
                    >
                      Slack's guide
                    </a>
                  </p>
                </div>
              )}

              {form.type === 'telegram' && (
                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                  {secretField('bot_token', 'dest-bot-token', 'Bot token', {
                    placeholder: existing?.bot_token_set ? 'Leave empty to keep current token' : examplePlaceholder('123456:ABC...'),
                  })}
                  {textField('chat_id', 'dest-chat-id', 'Chat ID')}
                </div>
              )}

              {form.type === 'webhook' && (
                <div className="grid gap-3">
                  {secretField('target_url', 'dest-target-url', 'Target URL', {
                    placeholder: existing?.target_url_set ? 'Leave empty to keep current URL' : examplePlaceholder('https://example.com/webhook'),
                  })}
                  {removeWebhookHeader ? (
                    <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-dashed p-3 text-body-sm text-fg-tertiary">
                      <span role="status">
                        The secret header {existing?.webhook_header_name ? `"${existing.webhook_header_name}" ` : ''}will be removed on save.
                      </span>
                      <Button type="button" variant="outline" size="sm" onClick={() => setRemoveWebhookHeader(false)}>
                        Keep it
                      </Button>
                    </div>
                  ) : (
                    <>
                      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                        {textField('webhook_header_name', 'dest-header-name', 'Secret header name', {
                          placeholder: examplePlaceholder('Authorization'),
                          optional: true,
                        })}
                        {secretField('webhook_header_value', 'dest-header-value', 'Secret header value', {
                          placeholder: existing?.webhook_header_name ? 'Leave empty to keep current value' : examplePlaceholder('Bearer …'),
                          optional: true,
                        })}
                      </div>
                      {/* The stored value could not be removed at all: an empty
                          box means "keep it", and clearing only the name left
                          the encrypted value orphaned. */}
                      {existing?.webhook_header_name && (
                        <div>
                          <Button type="button" variant="outline" size="sm" onClick={() => setRemoveWebhookHeader(true)}>
                            Remove secret header
                          </Button>
                        </div>
                      )}
                    </>
                  )}
                  <p className="text-body-sm text-fg-tertiary">
                    Alerts POST a JSON payload (project, rule, scan, message, items). The optional secret header is sent with every request — use it for auth (e.g. Authorization).
                  </p>
                </div>
              )}

              {form.type === 'email' && (
                <div className="grid gap-3">
                  {textField('email_recipients', 'dest-email-recipients', 'Recipients', {
                    placeholder: examplePlaceholder('alice@example.com', 'bob@example.com'),
                  })}
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                    {textField('email_from_address', 'dest-email-from', 'From address', {
                      placeholder: `${examplePlaceholder('alerts@tripl.example')} or Tripl Alerts <alerts@tripl.example>`,
                      optional: true,
                    })}
                    {textField('email_subject_template', 'dest-email-subject', 'Subject template', {
                      placeholder: examplePlaceholder(`[\${project_name}] \${rule_name}`),
                      optional: true,
                    })}
                  </div>
                  <p className="text-body-sm text-fg-tertiary">
                    SMTP settings (host/port/credentials) come from the instance config. Recipients are comma-separated. Subject supports {`\${project_name}`}, {`\${rule_name}`}, {`\${destination_name}`}, {`\${matched_count}`}.
                  </p>
                </div>
              )}

              {form.type === 'jira' && (
                <div className="grid gap-3">
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                    {textField('jira_base_url', 'dest-jira-base-url', 'Base URL', {
                      placeholder: examplePlaceholder('https://acme.atlassian.net'),
                    })}
                    {textField('jira_auth_email', 'dest-jira-auth-email', 'Auth email', {
                      placeholder: examplePlaceholder('alice@example.com'),
                    })}
                  </div>
                  {secretField('jira_api_token', 'dest-jira-api-token', 'API token', {
                    placeholder: existing?.jira_api_token_set ? 'Leave empty to keep current token' : 'Atlassian API token',
                  })}
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                    {textField('jira_project_key', 'dest-jira-project-key', 'Project key', {
                      placeholder: examplePlaceholder('ENG'),
                      onChange: event => set('jira_project_key', event.target.value.toUpperCase()),
                    })}
                    {textField('jira_issue_type', 'dest-jira-issue-type', 'Issue type', { placeholder: examplePlaceholder('Task') })}
                  </div>
                  {/* What happens, not how the API is called. */}
                  <p className="text-body-sm text-fg-tertiary">
                    Each alert opens a new issue in this Jira project. Sign in with your Atlassian
                    email and an API token from id.atlassian.com → Security → API tokens.
                  </p>
                </div>
              )}

              {form.type === 'linear' && (
                <div className="grid gap-3">
                  {secretField('linear_api_key', 'dest-linear-api-key', 'API key', {
                    placeholder: existing?.linear_api_key_set ? 'Leave empty to keep current key' : examplePlaceholder('lin_api_…'),
                  })}
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                    {textField('linear_team_id', 'dest-linear-team-id', 'Team ID', {
                      placeholder: 'Team UUID',
                    })}
                    {textField('linear_state_id', 'dest-linear-state-id', 'State ID', {
                      placeholder: 'Leave empty for the team default',
                      optional: true,
                    })}
                  </div>
                  {textField('linear_label_ids', 'dest-linear-label-ids', 'Label IDs', {
                    placeholder: `Comma-separated, ${examplePlaceholder('label-1, label-2')}`,
                    optional: true,
                  })}
                  {/* Where each value lives, not which GraphQL mutation runs.
                      Pickers that fetch teams, states and labels once
                      the key is in are the longer-term fix. */}
                  <p className="text-body-sm text-fg-tertiary">
                    Each alert opens a new issue in this Linear team. Create the API key in Linear
                    under Settings → API; the team ID is the team's UUID, shown in that team's
                    settings. Leave State ID empty to use the team's default state.
                  </p>
                </div>
              )}

              {form.type === 'pagerduty' && (
                <div className="grid gap-3">
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                    {secretField('pagerduty_routing_key', 'dest-pagerduty-routing-key', 'Integration key', {
                      placeholder: existing?.pagerduty_routing_key_set
                        ? 'Leave empty to keep current key'
                        : '32-character routing key',
                    })}
                    <div className="grid gap-2">
                      <Label htmlFor="dest-pagerduty-severity">Severity</Label>
                      <NativeSelect
                        id="dest-pagerduty-severity"
                        width="fill"
                        value={form.pagerduty_severity}
                        onChange={value => set('pagerduty_severity', value)}
                        options={PAGERDUTY_SEVERITY_OPTIONS}
                        {...fieldErrorProps('dest-pagerduty-severity', errorFor('pagerduty_severity'))}
                      />
                      <FieldError inputId="dest-pagerduty-severity" message={errorFor('pagerduty_severity')} />
                    </div>
                  </div>
                  {/* Where the key comes from, and the one behaviour no other
                      channel has: tripl closes the page it opened. */}
                  <p className="text-body-sm text-fg-tertiary">
                    In PagerDuty, add an <strong>Events API V2</strong> integration to a service
                    (Service → Integrations) and paste its integration key. Each incident pages
                    once and is updated, not duplicated, while it keeps firing; when tripl closes
                    it — the scope stops firing or someone resolves it in the Inbox — the page is
                    resolved too.
                  </p>
                </div>
              )}

              {form.type === 'teams' && (
                <div className="grid gap-2">
                  {secretField('teams_webhook_url', 'dest-teams-webhook-url', 'Webhook URL', {
                    placeholder: existing?.teams_webhook_set
                      ? 'Leave empty to keep current URL'
                      : examplePlaceholder('https://….webhook.office.com/…'),
                  })}
                  <p className="text-body-sm text-fg-tertiary">
                    In the Teams channel, add a Workflows flow from the template “Post to a channel
                    when a webhook request is received” (or an Incoming Webhook connector) and
                    paste the URL it gives you. Alerts arrive as an Adaptive Card.
                  </p>
                </div>
              )}

              {/* A local sink has no channel settings: it records deliveries on
                  this instance and sends nothing, so name, switch and schedule
                  are the whole form. It used to fall through to the Linear
                  fields, whose required API key blocked every save. */}
              {form.type === 'demo_sink' && (
                <p className="text-body-sm text-fg-tertiary">
                  A local sink records deliveries on this instance and sends nothing, so it has no channel settings.
                </p>
              )}

              <DeliveryScheduleField
                value={form.delivery_schedule_cron}
                onChange={cron => set('delivery_schedule_cron', cron)}
                onValidityChange={setScheduleValid}
                projectTimezone={resolveScheduleTimezone(project, existing)}
                nextDigestAt={existing?.next_digest_at}
                serverError={server.fields.delivery_schedule_cron}
              />

              <label className="flex items-center gap-2 text-body">
                <Checkbox
                  checked={form.enabled}
                  onCheckedChange={checked => set('enabled', !!checked)}
                />
                Destination enabled
              </label>

              {alertMessage && (
                <p role="alert" className="text-body text-destructive">{alertMessage}</p>
              )}

              {testIsCurrent && (
                <DestinationTestResult
                  pending={testMut.isPending}
                  result={testMut.data ?? null}
                  error={testMut.error}
                  onDismiss={() => setTestedWith(null)}
                />
              )}
            </DialogBody>
            <DialogFooter>
              {/* Left of the pair, and secondary: a check, not the action the
                  dialog is for. It sends one real message, so it
                  waits for nothing but the channel's own fields. */}
              {form.type !== 'demo_sink' && (
                <Button
                  type="button"
                  variant="outline"
                  className="sm:mr-auto"
                  disabled={testMut.isPending}
                  onClick={sendTest}
                >
                  <Send aria-hidden="true" className="size-3.5" />
                  {testMut.isPending ? 'Sending…' : 'Send test'}
                </Button>
              )}
              <Button type="button" variant="outline" onClick={requestClose}>Cancel</Button>
              <Button type="submit" disabled={mutation.isPending}>
                {existing ? 'Save' : 'Create'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  )
}
