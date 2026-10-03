import type { ReactNode } from 'react'
import { KeyRound } from 'lucide-react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { AiProviderPicker } from '@/components/settings/AiProviderPicker'
import { serviceSettingsApi, type AiPromptDefaults } from '@/api/serviceSettings'
import { aiPromptDefaultsKey } from '@/lib/queryKeys'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { getErrorMessage } from '@/lib/utils'
import type { ServiceSettings } from '@/types'
import { Button } from '@/components/ui/button'
import { Field, SCard, TextArea, TextInput, ToggleRow } from '@/components/settings/kit'
import { DisabledReason, disabledReasonAria } from '@/components/states'
import {
  InactiveGroup,
  NumberSettingInput,
  OperatorFields,
  SourceBadge,
  StatusBadge,
} from './ServiceSettingsPrimitives'
import type {
  EditableSettings,
  SecretDrafts,
  SecretField,
  SectionKey,
} from './serviceSettingsHelpers'
import { sourceFor } from './serviceSettingsHelpers'

export function AiSection({
  form,
  settings,
  secretDrafts,
  setField,
  setSecretDrafts,
  saving,
  onClearSecret,
  platformAdmin,
}: {
  form: EditableSettings
  settings: ServiceSettings
  secretDrafts: SecretDrafts
  setField: (section: SectionKey, field: string, value: string | number | boolean) => void
  setSecretDrafts: (updater: (current: SecretDrafts) => SecretDrafts) => void
  saving: boolean
  onClearSecret: (section: 'ai' | 'email', field: SecretField) => void
  /**
   * The endpoint and keys that receive every organization's plan text are
   * operator-only (backend `OPERATOR_FIELDS`) until each organization has its own.
   */
  platformAdmin: boolean
}) {
  // The built-in prompts behind each "Restore default" (ST-30). Silent: without
  // them the links simply do not appear.
  const defaultsQuery = useQuery({
    queryKey: aiPromptDefaultsKey(),
    queryFn: serviceSettingsApi.aiPromptDefaults,
    staleTime: Infinity,
    meta: SILENT_ERROR_META,
  })
  const promptLabelRight = (field: keyof AiPromptDefaults, label: string) => (
    <>
      <SourceBadge source={sourceFor(settings, 'ai', field)} />
      <RestoreDefault
        label={label}
        value={form.ai[field]}
        defaultValue={defaultsQuery.data?.[field]}
        onRestore={value => setField('ai', field, value)}
      />
    </>
  )
  const aiTestMut = useMutation({
    mutationFn: () => serviceSettingsApi.testAi(),
    // A failed request is rendered in the status slot beside the button.
    meta: SILENT_ERROR_META,
  })
  // The test reads the SAVED settings, so it is the saved switch that decides
  // whether it can pass: pressed with AI off it could only answer "AI is
  // disabled or no API key is configured" (ST-26). The key flag already
  // counts the OPENAI_API_KEY fallback, so it is the key the test would use.
  const testBlocker = !settings.ai.ai_enabled
    ? 'AI is off in the saved settings. Turn it on and save first.'
    : !settings.ai.ai_api_key_configured
      ? 'No API key is saved. Add one and save first.'
      : null

  return (
    <>
      <SCard title="Provider">
        <ToggleRow
          label="AI enabled"
          labelRight={<SourceBadge source={sourceFor(settings, 'ai', 'ai_enabled')} />}
          value={form.ai.ai_enabled}
          onChange={value => setField('ai', 'ai_enabled', value)}
        />
        {/* Still editable — preparing a config before switching it on is
            valid — but visibly idle while the switch is off (ST-26). */}
        <InactiveGroup inactive={!form.ai.ai_enabled} reason="Not used while AI is off.">
        <OperatorFields locked={!platformAdmin}>
        <AiProviderPicker
          baseUrl={form.ai.ai_base_url}
          model={form.ai.ai_model}
          onChange={next => {
            setField('ai', 'ai_base_url', next.baseUrl)
            setField('ai', 'ai_model', next.model)
          }}
        />
        <Field
          label="Base URL"
          labelRight={<SourceBadge source={sourceFor(settings, 'ai', 'ai_base_url')} />}
        >
          <TextInput
            value={form.ai.ai_base_url}
            onChange={value => setField('ai', 'ai_base_url', value)}
            mono
          />
        </Field>
        </OperatorFields>
        <Field
          label="Model"
          labelRight={<SourceBadge source={sourceFor(settings, 'ai', 'ai_model')} />}
        >
          <TextInput
            value={form.ai.ai_model}
            onChange={value => setField('ai', 'ai_model', value)}
            mono
          />
        </Field>
        {/* Badged like every other resettable field. A stored key IS an override
            — overrideCount counts it and Reset nulls it — but the row rendered
            nothing, so an instance whose only override was this key showed a red
            "Clears the 1 AI override" card above rows that all read "Default"
            (tripl-5qp9 / tripl-wkwv.2). The source says override/env/default and
            nothing about the value; the *_configured placeholder beside it
            already reveals more. */}
        <OperatorFields locked={!platformAdmin} quiet>
        <Field
          label="AI API key"
          labelRight={<SourceBadge source={sourceFor(settings, 'ai', 'ai_api_key')} />}
        >
          <div className="flex gap-2">
            <div className="flex-1">
              <TextInput
                type="password"
                value={secretDrafts.ai_api_key}
                onChange={value =>
                  setSecretDrafts(current => ({ ...current, ai_api_key: value }))
                }
                placeholder={
                  form.ai.ai_api_key_configured
                    ? 'Configured — leave blank to keep'
                    : 'Not configured'
                }
              />
            </div>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => onClearSecret('ai', 'ai_api_key')}
              // Nothing stored, nothing to delete: this opened a red "start
              // failing at once" confirm for a key that did not exist.
              disabled={saving || !form.ai.ai_api_key_configured}
            >
              Delete stored key
            </Button>
          </div>
        </Field>
        </OperatorFields>
        {/* A test button and its status line, not a control to be named. The
            request carries no draft values, so it checks what is stored — said
            here the way the Email "Check" card says it. */}
        <Field
          label="Connection"
          last
          htmlFor={false}
          hint="Uses the saved settings, so save your changes first."
        >
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => aiTestMut.mutate()}
              disabled={aiTestMut.isPending || testBlocker !== null}
              {...disabledReasonAria('ai-test', testBlocker)}
            >
              <KeyRound className="h-3.5 w-3.5" />
              {aiTestMut.isPending ? 'Testing...' : 'Test AI'}
            </Button>
            <span role="status" aria-live="polite" aria-atomic="true" className="inline-flex">
              {aiTestMut.isError ? (
                <StatusBadge active={false} label={getErrorMessage(aiTestMut.error)} />
              ) : (
                aiTestMut.data && (
                  <StatusBadge active={aiTestMut.data.ok} label={aiTestMut.data.message} />
                )
              )}
            </span>
          </div>
          <DisabledReason id="ai-test" reason={testBlocker} tone="muted" className="mt-1.5" />
        </Field>
        </InactiveGroup>
      </SCard>

      <SCard title="Generation">
        <Field
          label="Timeout seconds"
          labelRight={<SourceBadge source={sourceFor(settings, 'ai', 'ai_timeout_seconds')} />}
        >
          <NumberSettingInput
            section="ai"
            field="ai_timeout_seconds"
            value={form.ai.ai_timeout_seconds}
            saved={settings.ai.ai_timeout_seconds}
            setField={setField}
            suffix="seconds"
          />
        </Field>
        <Field
          label="Max output tokens"
          labelRight={<SourceBadge source={sourceFor(settings, 'ai', 'ai_max_output_tokens')} />}
        >
          <NumberSettingInput
            section="ai"
            field="ai_max_output_tokens"
            value={form.ai.ai_max_output_tokens}
            saved={settings.ai.ai_max_output_tokens}
            setField={setField}
          />
        </Field>
        <Field
          label="Describe prompt"
          labelRight={promptLabelRight('describe_system_prompt', 'Describe prompt')}
          stacked
        >
          {/* Prose, so the body font, and a box that grows with it: four
              fixed lines cut a prompt mid-line (ST-30). */}
          <TextArea
            value={form.ai.describe_system_prompt}
            onChange={value => setField('ai', 'describe_system_prompt', value)}
            rows={6}
            autoGrow
          />
        </Field>
        <Field
          label="Ask prompt"
          labelRight={promptLabelRight('ask_system_prompt', 'Ask prompt')}
          stacked
        >
          <TextArea
            value={form.ai.ask_system_prompt}
            onChange={value => setField('ai', 'ask_system_prompt', value)}
            rows={6}
            autoGrow
          />
        </Field>
        <Field
          label="Alert explanation prompt"
          labelRight={promptLabelRight('alert_explanation_system_prompt', 'Alert explanation prompt')}
          stacked
          last
        >
          <TextArea
            value={form.ai.alert_explanation_system_prompt}
            onChange={value => setField('ai', 'alert_explanation_system_prompt', value)}
            rows={6}
            autoGrow
          />
        </Field>
      </SCard>

      <SCard title="Search embeddings">
        <ToggleRow
          label="Search embeddings"
          labelRight={
            <SourceBadge source={sourceFor(settings, 'ai', 'search_embeddings_enabled')} />
          }
          value={form.ai.search_embeddings_enabled}
          onChange={value => setField('ai', 'search_embeddings_enabled', value)}
        />
        <InactiveGroup
          inactive={!form.ai.search_embeddings_enabled}
          reason="Not used while Search embeddings is off."
        >
        {/* Where indexed plan text is actually POSTed. It was configurable but
            unreportable: nothing in the running system said which endpoint the
            vectors came from, so a compose allowlist slip that dropped
            SEARCH_EMBEDDING_BASE_URL sent the text to OpenAI with nothing to
            notice it by (tripl-wkwv.2). Shown, never editable — and its badge is
            what answers "did the variable reach this container?", which is the
            whole reason the row exists. */}
        <Field
          label="Embeddings base URL"
          labelRight={
            <SourceBadge source={sourceFor(settings, 'ai', 'search_embedding_base_url')} />
          }
          // One line and a "Why?", not an eight-line essay beside one value
          // (ST-30); the value is text, not a dashed input that cannot move.
          hint={
            <EnvOnlyHint variable="SEARCH_EMBEDDING_BASE_URL">
              Every indexed event name, description and field value is POSTed here. The vectors
              already in the index came from whatever endpoint produced them, and similarity across
              two embedding spaces is meaningless — changing this is a re-embed and a deploy, not a
              setting.
            </EnvOnlyHint>
          }
          htmlFor={false}
        >
          <EnvOnlyValue value={form.ai.search_embedding_base_url} />
        </Field>
        {/* The other inert control on this page. It carried `disabled` and
            nothing else — no badge, no hint — so it read as an editable number
            sitting in a row of editable numbers. Say why it cannot move. */}
        <Field
          label="Embedding dimensions"
          labelRight={
            <SourceBadge source={sourceFor(settings, 'ai', 'search_embedding_dimensions')} />
          }
          hint={
            <EnvOnlyHint variable="SEARCH_EMBEDDING_DIMENSIONS">
              The vectors already in the index were written at this width, and similarity across two
              widths is meaningless — changing it is a re-embed and a deploy, not a setting.
            </EnvOnlyHint>
          }
          htmlFor={false}
        >
          <EnvOnlyValue value={String(form.ai.search_embedding_dimensions)} />
        </Field>
        <OperatorFields locked={!platformAdmin}>
        <Field
          label="Embedding provider"
          labelRight={<SourceBadge source={sourceFor(settings, 'ai', 'search_embedding_provider')} />}
        >
          <TextInput
            value={form.ai.search_embedding_provider}
            onChange={value => setField('ai', 'search_embedding_provider', value)}
            mono
          />
        </Field>
        <Field
          label="Embedding model"
          labelRight={<SourceBadge source={sourceFor(settings, 'ai', 'search_embedding_model')} />}
        >
          <TextInput
            value={form.ai.search_embedding_model}
            onChange={value => setField('ai', 'search_embedding_model', value)}
            mono
          />
        </Field>
        <Field
          label="Embedding API key"
          labelRight={
            <SourceBadge source={sourceFor(settings, 'ai', 'search_embedding_api_key')} />
          }
          last
        >
          <div className="flex gap-2">
            <div className="flex-1">
              <TextInput
                type="password"
                value={secretDrafts.search_embedding_api_key}
                onChange={value =>
                  setSecretDrafts(current => ({
                    ...current,
                    search_embedding_api_key: value,
                  }))
                }
                placeholder={
                  form.ai.search_embedding_api_key_configured
                    ? 'Configured — leave blank to keep'
                    : 'Not configured'
                }
              />
            </div>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => onClearSecret('ai', 'search_embedding_api_key')}
              disabled={saving || !form.ai.search_embedding_api_key_configured}
            >
              Delete stored key
            </Button>
          </div>
        </Field>
        </OperatorFields>
        </InactiveGroup>
      </SCard>
    </>
  )
}

/** "Env-only: VAR. Changing it needs a re-embed." with the reasoning folded away (ST-30). */
function EnvOnlyHint({ variable, children }: { variable: string; children: ReactNode }) {
  return (
    <>
      Env-only: <code className="mono">{variable}</code>. Changing it needs a re-embed.{' '}
      <details className="mt-1">
        <summary className="cursor-pointer text-accent">Why?</summary>
        <p className="m-0 mt-1">{children}</p>
      </details>
    </>
  )
}

/** A value the environment sets and this page only reports: text, not an input. */
function EnvOnlyValue({ value }: { value: string }) {
  return (
    <span className="mono block truncate pt-1.5 text-body-sm" title={value}>
      {value || '—'}
    </span>
  )
}

/**
 * "Restore default" beside a prompt that differs from the built-in one (ST-30).
 * It fills the editor; Save stores it like any other edit.
 */
function RestoreDefault({
  label,
  value,
  defaultValue,
  onRestore,
}: {
  label: string
  value: string
  defaultValue: string | undefined
  onRestore: (value: string) => void
}) {
  if (defaultValue === undefined || value === defaultValue) return null
  return (
    <button
      type="button"
      className="text-caption text-accent hover:underline"
      aria-label={`Restore the default ${label.toLowerCase()}`}
      onClick={() => onRestore(defaultValue)}
    >
      Restore default
    </button>
  )
}
