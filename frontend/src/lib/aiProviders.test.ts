import { describe, expect, it } from 'vitest'
import { aiProviderForBaseUrl, applyAiProvider } from './aiProviders'

describe('aiProviderForBaseUrl', () => {
  it('recognises each preset, tolerating a trailing slash and case', () => {
    expect(aiProviderForBaseUrl('https://api.openai.com/v1')).toBe('openai')
    expect(aiProviderForBaseUrl('https://api.anthropic.com/v1/')).toBe('anthropic')
    expect(aiProviderForBaseUrl('https://generativelanguage.googleapis.com/v1beta/openai')).toBe('gemini')
    expect(aiProviderForBaseUrl('HTTPS://OPENROUTER.AI/api/v1')).toBe('openrouter')
  })

  it('calls anything else custom', () => {
    expect(aiProviderForBaseUrl('https://llm.internal.example/v1')).toBe('custom')
    expect(aiProviderForBaseUrl('')).toBe('custom')
    expect(aiProviderForBaseUrl(null)).toBe('custom')
  })
})

describe('applyAiProvider', () => {
  it('fills the base URL and swaps a preset default model for the new one', () => {
    expect(applyAiProvider('anthropic', { baseUrl: 'https://api.openai.com/v1', model: 'gpt-4o-mini' })).toEqual({
      baseUrl: 'https://api.anthropic.com/v1',
      model: 'claude-haiku-4-5',
    })
    expect(applyAiProvider('gemini', { baseUrl: '', model: '' }).model).toBe('gemini-2.5-flash')
  })

  it('keeps a model the reader chose', () => {
    expect(applyAiProvider('openrouter', { baseUrl: '', model: 'anthropic/claude-sonnet-4.5' })).toEqual({
      baseUrl: 'https://openrouter.ai/api/v1',
      model: 'anthropic/claude-sonnet-4.5',
    })
  })

  it('leaves the fields alone for custom', () => {
    const current = { baseUrl: 'https://x.example/v1', model: 'm' }
    expect(applyAiProvider('custom', current)).toBe(current)
  })
})
