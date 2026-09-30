import { useId } from 'react'
import type { SsoConfig } from '@/api/sso'
import { Field, TextArea, TextInput } from '@/components/settings/kit'
import { ssoDisplayValue, ssoFieldError, type SsoDraft, type SsoTextField } from './orgSsoModel'

/**
 * One provider field of Organization › Single sign-on (F20): the draft's value
 * over the saved one, with the draft's problem under it.
 */
export function SsoTextInput({
  config,
  draft,
  setValue,
  field,
  label,
  placeholder,
  hint,
  secret,
  multiline,
  last,
}: {
  config: SsoConfig
  draft: SsoDraft
  setValue: (field: SsoTextField, value: string) => void
  field: SsoTextField
  label: string
  placeholder?: string
  hint?: string
  secret?: boolean
  /** A certificate box: several lines, stacked under its label. */
  multiline?: boolean
  last?: boolean
}) {
  const errorId = useId()
  const error = ssoFieldError(field, draft)
  const value = ssoDisplayValue(config, draft, field)
  const errorLine = error && (
    <p id={errorId} className="mt-1 text-caption text-danger">
      {error}
    </p>
  )
  if (multiline) {
    return (
      <Field label={label} hint={hint} last={last} stacked>
        <TextArea
          value={value}
          onChange={(next) => setValue(field, next)}
          placeholder={placeholder}
          rows={6}
          autoGrow
          mono
          aria-invalid={error !== null}
          aria-describedby={error ? errorId : undefined}
        />
        {errorLine}
      </Field>
    )
  }
  return (
    <Field label={label} hint={hint} last={last}>
      <TextInput
        type={secret ? 'password' : 'text'}
        autoComplete="off"
        value={value}
        onChange={(next) => setValue(field, next)}
        placeholder={placeholder}
        mono={!secret}
        aria-invalid={error !== null}
        aria-describedby={error ? errorId : undefined}
      />
      {errorLine}
    </Field>
  )
}
