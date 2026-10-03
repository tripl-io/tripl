/**
 * Chat providers the AI settings offer as presets. tripl talks to every one of
 * them through its OpenAI-compatible endpoint (`POST {base}/chat/completions`
 * with a Bearer key), so a preset is only a base URL and a starting model: it
 * fills the two fields, and both stay editable.
 */
export type AiProviderId = 'openai' | 'anthropic' | 'gemini' | 'openrouter' | 'custom'

export interface AiProviderPreset {
  id: Exclude<AiProviderId, 'custom'>
  label: string
  baseUrl: string
  /** A small, cheap model: describe, ask and alert explanations are short. */
  defaultModel: string
  /** Where the key comes from, for the hint under the picker. */
  keyHint: string
}

export const AI_PROVIDER_PRESETS: readonly AiProviderPreset[] = [
  {
    id: 'openai',
    label: 'OpenAI',
    baseUrl: 'https://api.openai.com/v1',
    defaultModel: 'gpt-4o-mini',
    keyHint: 'An API key from platform.openai.com.',
  },
  {
    id: 'anthropic',
    label: 'Anthropic (Claude)',
    baseUrl: 'https://api.anthropic.com/v1',
    defaultModel: 'claude-haiku-4-5',
    keyHint: 'An API key from console.anthropic.com. Claude has no embeddings: keep search on another provider.',
  },
  {
    id: 'gemini',
    label: 'Google Gemini',
    baseUrl: 'https://generativelanguage.googleapis.com/v1beta/openai',
    defaultModel: 'gemini-2.5-flash',
    keyHint: 'An API key from Google AI Studio (aistudio.google.com).',
  },
  {
    id: 'openrouter',
    label: 'OpenRouter',
    baseUrl: 'https://openrouter.ai/api/v1',
    defaultModel: 'google/gemini-2.5-flash',
    keyHint: 'An API key from openrouter.ai. Models are named vendor/model, e.g. anthropic/claude-haiku-4.5.',
  },
]

function normalizeUrl(url: string): string {
  return url.trim().replace(/\/+$/, '').toLowerCase()
}

/** The preset a base URL belongs to, or `custom` for any other endpoint. */
export function aiProviderForBaseUrl(baseUrl: string | null | undefined): AiProviderId {
  const normalized = normalizeUrl(baseUrl ?? '')
  if (!normalized) return 'custom'
  return AI_PROVIDER_PRESETS.find(preset => normalizeUrl(preset.baseUrl) === normalized)?.id ?? 'custom'
}

export function aiProviderPreset(id: AiProviderId): AiProviderPreset | undefined {
  return AI_PROVIDER_PRESETS.find(preset => preset.id === id)
}

/**
 * The fields to write when the reader picks `id`: its base URL, and its model
 * unless the current model is one the reader chose themselves (not empty and
 * not another preset's default).
 */
export function applyAiProvider(
  id: AiProviderId,
  current: { baseUrl: string; model: string },
): { baseUrl: string; model: string } {
  const preset = aiProviderPreset(id)
  if (!preset) return current
  const model = current.model.trim()
  const presetDefault = AI_PROVIDER_PRESETS.some(candidate => candidate.defaultModel === model)
  return {
    baseUrl: preset.baseUrl,
    model: !model || presetDefault ? preset.defaultModel : current.model,
  }
}
