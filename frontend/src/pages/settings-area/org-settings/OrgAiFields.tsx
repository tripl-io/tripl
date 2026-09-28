import { KeyRound } from 'lucide-react'
import { useMutation } from '@tanstack/react-query'
import { orgSettingsApi } from '@/api/orgSettings'
import { Button } from '@/components/ui/button'
import { Field, SCard, TextArea, TextInput, ToggleRow } from '@/components/settings/kit'
import { DisabledReason, disabledReasonAria } from '@/components/states'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { getErrorMessage } from '@/lib/utils'
import { StatusBadge } from '@/pages/settings-service/ServiceSettingsPrimitives'
import {
  AI_ENDPOINT_GROUP,
  clearGroup,
  displayValue,
  groupOwned,
  groupWarning,
  sourceOf,
  type OrgDraft,
} from './orgSettingsModel'
import { InheritHint, OrgSourceBadge, OrgTextField, type OrgFieldProps } from './OrgSettingsPrimitives'

const PROMPTS = [
  { field: 'describe_system_prompt', label: 'Describe prompt' },
  { field: 'ask_system_prompt', label: 'Ask prompt' },
  { field: 'alert_explanation_system_prompt', label: 'Alert explanation prompt' },
] as const

/**
 * Organization › AI: the chat provider this organization's plan text is sent
 * to, its prompts, and its timeouts within the operator's maxima. Search
 * embeddings stay the platform's.
 */
export function OrgAiFields({
  org,
  settings,
  draft,
  setField,
  setDraft,
  saving,
}: OrgFieldProps & {
  org: string
  setDraft: (next: OrgDraft) => void
  saving: boolean
}) {
  const section = 'ai' as const
  const props = { settings, draft, setField, section }
  const warning = groupWarning(settings, draft, section)
  const owned = settings.scope === 'organization' && groupOwned(settings, section, AI_ENDPOINT_GROUP)
  const enabled = displayValue(settings, draft, section, 'ai_enabled') === true
  const organizationScope = settings.scope === 'organization'
  const testMut = useMutation({
    mutationFn: () => orgSettingsApi.testAi(org),
    meta: SILENT_ERROR_META,
  })
  const testBlocker = !settings.ai.ai_enabled
    ? 'AI is off for this organization in the saved settings. Turn it on and save first.'
    : !settings.ai.ai_api_key_configured
      ? 'No API key is in effect for this organization. Add one and save first.'
      : null

  return (
    <>
      <SCard
        title="Provider"
        description="Base URL, model and key are one group: once this organization sets any of them it stops inheriting the rest, and the operator's key is never sent to its endpoint."
        footer={
          owned ? (
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setDraft(clearGroup(draft, settings, section, AI_ENDPOINT_GROUP))}
              disabled={saving}
            >
              Use the operator's provider
            </Button>
          ) : undefined
        }
      >
        <ToggleRow
          label="AI enabled"
          labelRight={<OrgSourceBadge source={sourceOf(settings, section, 'ai_enabled')} />}
          hint={<InheritHint {...props} field="ai_enabled" />}
          value={enabled}
          onChange={value => setField('ai_enabled', value)}
        />
        {warning && (
          <p role="note" className="m-0 px-4 pt-2.5 text-caption text-(--warning)">
            {warning.starting &&
              'Saving makes the whole provider this organization’s own: fields you leave as they are take the built-in defaults, not the operator’s values. '}
            {warning.missingSecret &&
              'No API key of this organization’s: the operator’s key is never sent to an endpoint set here, so add one.'}
          </p>
        )}
        <OrgTextField
          {...props}
          field="ai_base_url"
          label="Base URL"
          grouped
          placeholder="e.g. https://api.openai.com/v1"
          hint={organizationScope ? 'Must be a public address.' : undefined}
        />
        <OrgTextField {...props} field="ai_model" label="Model" grouped placeholder="e.g. gpt-4o-mini" />
        <Field
          label="API key"
          labelRight={<OrgSourceBadge source={sourceOf(settings, section, 'ai_api_key')} />}
          hint={<InheritHint {...props} field="ai_api_key" grouped />}
        >
          <TextInput
            type="password"
            value={String(displayValue(settings, draft, section, 'ai_api_key'))}
            onChange={value => setField('ai_api_key', value)}
            placeholder={settings.ai.ai_api_key_configured ? 'Configured — leave blank to keep' : 'Not configured'}
          />
        </Field>
        <OrgTextField
          {...props}
          field="ai_timeout_seconds"
          label="Timeout"
          number
          suffix="s"
          hint={organizationScope ? `Operator maximum: ${settings.ceilings.ai_timeout_seconds} s.` : undefined}
        />
        <OrgTextField
          {...props}
          field="ai_max_output_tokens"
          label="Max output tokens"
          number
          hint={
            organizationScope
              ? `Operator maximum: ${settings.ceilings.ai_max_output_tokens.toLocaleString('en-US')}.`
              : undefined
          }
          last
        />
      </SCard>

      <SCard title="Prompts" description="The system prompts this organization's AI features send.">
        {PROMPTS.map(({ field, label }, index) => (
          <Field
            key={field}
            label={label}
            stacked
            labelRight={<OrgSourceBadge source={sourceOf(settings, section, field)} />}
            hint={<InheritHint {...props} field={field} />}
            last={index === PROMPTS.length - 1}
          >
            <TextArea
              value={String(displayValue(settings, draft, section, field))}
              onChange={value => setField(field, value)}
              rows={4}
              autoGrow
            />
          </Field>
        ))}
      </SCard>

      <SCard
        title="Test the provider"
        description="Sends one short prompt with the saved settings this organization uses, so save your changes first."
      >
        <Field label="Connection" last htmlFor={false}>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => testMut.mutate()}
              disabled={testMut.isPending || saving || testBlocker !== null}
              {...disabledReasonAria('org-ai-test', testBlocker)}
            >
              <KeyRound className="h-3.5 w-3.5" />
              {testMut.isPending ? 'Testing...' : 'Test AI'}
            </Button>
            <span role="status" aria-live="polite" aria-atomic="true" className="inline-flex">
              {testMut.isError ? (
                <StatusBadge active={false} label={getErrorMessage(testMut.error)} />
              ) : (
                testMut.data && <StatusBadge active={testMut.data.ok} label={testMut.data.message} />
              )}
            </span>
          </div>
          <DisabledReason id="org-ai-test" reason={testBlocker} className="mt-1.5" />
        </Field>
      </SCard>
    </>
  )
}
