import { expect, signInAsOwner, test } from './fixtures'

/**
 * AI provider presets: an organization owner picks Claude, Gemini or
 * OpenRouter in Organization › AI and the base URL and model fill in, ready
 * for their key. Nothing is saved, so no request leaves for a real provider.
 */
test('picking a provider fills the AI base URL and model', async ({ page }) => {
  await signInAsOwner(page)
  await page.goto('/settings/organization/ai')

  const provider = page.getByLabel('AI provider')
  const baseUrl = page.getByLabel('Base URL')
  const model = page.getByLabel('Model', { exact: true })
  // A route's first visit compiles it on the dev server: give it time.
  await expect(provider).toBeVisible({ timeout: 60_000 })

  await provider.selectOption('anthropic')
  await expect(baseUrl).toHaveValue('https://api.anthropic.com/v1')
  await expect(model).toHaveValue('claude-haiku-4-5')
  await expect(page.getByText(/console\.anthropic\.com/)).toBeVisible()

  await provider.selectOption('gemini')
  await expect(baseUrl).toHaveValue('https://generativelanguage.googleapis.com/v1beta/openai')
  await expect(model).toHaveValue('gemini-2.5-flash')

  // A model the reader typed survives a provider switch.
  await model.fill('anthropic/claude-sonnet-4.5')
  await provider.selectOption('openrouter')
  await expect(baseUrl).toHaveValue('https://openrouter.ai/api/v1')
  await expect(model).toHaveValue('anthropic/claude-sonnet-4.5')

  // Editing the URL by hand reads back as a custom endpoint.
  await baseUrl.fill('https://llm.example.com/v1')
  await expect(provider).toHaveValue('custom')
})
