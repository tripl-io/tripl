import { describe, expect, it } from 'vitest'
import { CURRENT_DEMO_RECIPE_VERSION, isOutdatedDemoRecipe } from './demoRecipe'

describe('isOutdatedDemoRecipe', () => {
  it('calls a demo from an older recipe outdated, in either spelling', () => {
    expect(isOutdatedDemoRecipe('4')).toBe(true)
    expect(isOutdatedDemoRecipe('v3')).toBe(true)
    expect(isOutdatedDemoRecipe(' V1 ')).toBe(true)
  })

  it('leaves the current recipe, and any newer one, alone', () => {
    expect(isOutdatedDemoRecipe(String(CURRENT_DEMO_RECIPE_VERSION))).toBe(false)
    expect(isOutdatedDemoRecipe(`v${CURRENT_DEMO_RECIPE_VERSION + 1}`)).toBe(false)
  })

  it('does not nag about a version nobody can name', () => {
    expect(isOutdatedDemoRecipe(null)).toBe(false)
    expect(isOutdatedDemoRecipe(undefined)).toBe(false)
    expect(isOutdatedDemoRecipe('')).toBe(false)
    expect(isOutdatedDemoRecipe('nightly')).toBe(false)
  })
})
