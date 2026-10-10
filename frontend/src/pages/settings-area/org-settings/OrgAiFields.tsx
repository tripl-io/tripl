import { KeyRound } from 'lucide-react'
import { useMutation } from '@tanstack/react-query'
import { orgSettingsApi } from '@/api/orgSettings'
import { AiProviderPicker } from '@/components/settings/AiProviderPicker'
import { Button } from '@/components/ui/button'
import { Field, SCard, TextArea, TextInput, ToggleRow } from '@/components/settings/kit'
import { DisabledReason, disabledReasonAria } from '@/components/states'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { getErrorMessage } from '@/lib/utils'
import { FIELD_COPY, PROMPT_FIELDS } from '@/pages/settings-service/fieldCopy'
import { StatusBadge } from '@/pages/settings-service/ServiceSettingsPrimitives'
import {
  AI_ENDPOINT_GROUP,
  clearGroup,
  displayValue,
  groupOwned,
  groupWarning,
  type OrgDraft,
} from './orgSettingsModel'
import { InheritHint, OrgFieldBadge, OrgTextField, type OrgFieldProps } from './OrgSettingsPrimitives'
import { formatNumber } from '@/lib/format'

/**
 * Organization › AI: the chat provider this organization's plan text is sent
 * to, its prompts, and its timeouts within the platform's maxima. Search
 * embeddings are their own section (OrgSearchFields).
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
        // The group rule is about inheriting; the platform's own provider (the
        // self-hosted default organization's page) inherits from nothing.
        description={
          organizationScope
            ? "Base URL, model and key are one group: once this organization sets any of them it stops inheriting the rest, and the platform's key is never sent to its endpoint."
            : undefined
        }
        footer={
          owned ? (
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setDraft(clearGroup(draft, settings, section, AI_ENDPOINT_GROUP))}
              disabled={saving}
            >
              Use the platform&rsquo;s provider
            </Button>
          ) : undefined
        }
      >
        <ToggleRow
          label={FIELD_COPY.ai_enabled.label}
          labelRight={<OrgFieldBadge settings={settings} section={section} field="ai_enabled" />}
          hint={<InheritHint {...props} field="ai_enabled" />}
          value={enabled}
          onChange={value => setField('ai_enabled', value)}
        />
        {warning && (
          <p role="note" className="m-0 px-4 pt-2.5 text-caption text-(--warning)">
            {warning.starting &&
              'Saving makes the whole provider this organization’s own: fields you leave as they are take the built-in defaults, not the platform’s values. '}
            {warning.missingSecret &&
              'No API key of this organization’s: the platform’s key is never sent to an endpoint set here, so add one.'}
          </p>
        )}
        <AiProviderPicker
          baseUrl={String(displayValue(settings, draft, section, 'ai_base_url') ?? '')}
          model={String(displayValue(settings, draft, section, 'ai_model') ?? '')}
          onChange={next => {
            setField('ai_base_url', next.baseUrl)
            setField('ai_model', next.model)
          }}
        />
        <OrgTextField
          {...props}
          field="ai_base_url"
          label={FIELD_COPY.ai_base_url.label}
          grouped
          placeholder="e.g. https://api.openai.com/v1"
          hint={organizationScope ? 'Must be a public address.' : undefined}
        />
        <OrgTextField
          {...props}
          field="ai_model"
          label={FIELD_COPY.ai_model.label}
          grouped
          placeholder="e.g. gpt-4o-mini"
        />
        <Field
          label={FIELD_COPY.ai_api_key.label}
          labelRight={<OrgFieldBadge settings={settings} section={section} field="ai_api_key" />}
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
          label={FIELD_COPY.ai_timeout_seconds.label}
          number
          suffix={FIELD_COPY.ai_timeout_seconds.suffix}
          hint={
            organizationScope
              ? `Platform maximum: ${formatNumber(settings.ceilings.ai_timeout_seconds)} seconds.`
              : undefined
          }
        />
        <OrgTextField
          {...props}
          field="ai_max_output_tokens"
          label={FIELD_COPY.ai_max_output_tokens.label}
          number
          hint={
            organizationScope
              ? `Platform maximum: ${formatNumber(settings.ceilings.ai_max_output_tokens)}.`
              : undefined
          }
          last
        />
      </SCard>

      <SCard title="Prompts" description="The system prompts this organization's AI features send.">
        {PROMPT_FIELDS.map((field, index) => (
          <Field
            key={field}
            label={FIELD_COPY[field].label}
            stacked
            labelRight={<OrgFieldBadge settings={settings} section={section} field={field} />}
            hint={<InheritHint {...props} field={field} />}
            last={index === PROMPT_FIELDS.length - 1}
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
