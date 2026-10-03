import { Field, NativeSelect } from '@/components/settings/kit'
import {
  AI_PROVIDER_PRESETS,
  aiProviderForBaseUrl,
  aiProviderPreset,
  applyAiProvider,
  type AiProviderId,
} from '@/lib/aiProviders'

const OPTIONS = [
  ...AI_PROVIDER_PRESETS.map(preset => ({ value: preset.id, label: preset.label })),
  { value: 'custom', label: 'Custom (OpenAI-compatible)' },
]

/**
 * A provider picker above the Base URL and Model fields: picking Claude,
 * Gemini or OpenRouter fills both with that provider's OpenAI-compatible
 * endpoint and a starting model. It reads the provider back from the base URL,
 * so an edited URL shows "Custom".
 */
export function AiProviderPicker({
  baseUrl,
  model,
  onChange,
  disabled,
}: {
  baseUrl: string
  model: string
  onChange: (next: { baseUrl: string; model: string }) => void
  disabled?: boolean
}) {
  const provider = aiProviderForBaseUrl(baseUrl)
  const preset = aiProviderPreset(provider)
  return (
    <Field
      label="Provider"
      hint={preset ? preset.keyHint : 'Any endpoint that serves the OpenAI chat completions API.'}
    >
      <NativeSelect
        value={provider}
        options={OPTIONS}
        disabled={disabled}
        aria-label="AI provider"
        onChange={value => {
          if (value === 'custom') return
          onChange(applyAiProvider(value as AiProviderId, { baseUrl, model }))
        }}
      />
    </Field>
  )
}
