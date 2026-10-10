import { describe, expect, it } from 'vitest'
import { PLATFORM_SECTION_KEYS, PLATFORM_SECTION_LABELS } from '@/components/settings/platform-sections'
import { embeddingProviderText, inactiveBackendNote, PHOTO_BACKEND_OPTIONS } from './fieldCopy'
import { SECTION_KEYS, dirtySections, resetCardDescription, resetConfirm } from './serviceSettingsHelpers'

describe('the search-embedding provider', () => {
  it('reads as the API tripl speaks, not as an internal token', () => {
    expect(embeddingProviderText('openai')).toBe('OpenAI-compatible API')
    // Unset falls back to the same one.
    expect(embeddingProviderText('')).toBe('OpenAI-compatible API')
  })

  it('names any other value as unsupported, since nothing gets embedded with it', () => {
    expect(embeddingProviderText('cohere')).toMatch(/^cohere \(unsupported/)
  })
})

describe('the photo backends', () => {
  it('are offered in one order, under one name each, on both settings pages', () => {
    expect(PHOTO_BACKEND_OPTIONS.map(option => option.value)).toEqual(['local', 'gcs'])
    expect(PHOTO_BACKEND_OPTIONS.map(option => option.label)).toEqual([
      "The server's disk",
      'Google Cloud Storage bucket',
    ])
  })

  it('say where photos go when the other backend’s fields are idle', () => {
    expect(inactiveBackendNote('local')).toMatch(/photos go to the server's disk/)
    expect(inactiveBackendNote('gcs')).toMatch(/photos go to the Google Cloud Storage bucket/)
  })
})

describe('the Platform sections with settings', () => {
  it('are the rail’s, in rail order, without the read-only System page', () => {
    expect(SECTION_KEYS).toEqual(PLATFORM_SECTION_KEYS.filter(key => key !== 'system'))
  })

  it('are listed as unsaved in rail order', () => {
    expect(dirtySections({ storage: { photo_max_size_mb: 5 }, email: { smtp_port: 25 } })).toEqual([
      'email',
      'storage',
    ])
  })

  it('go by the rail’s names in the reset card and its confirm', () => {
    expect(resetCardDescription('email', 1)).toMatch(/^Clears the 1 Mail relay override/)
    expect(resetConfirm('ai').title).toBe(`Reset ${PLATFORM_SECTION_LABELS.ai} to defaults`)
  })
})
