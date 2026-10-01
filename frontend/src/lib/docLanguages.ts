/** The `lang` a read passes for the original (not a language code). */
export const ORIGINAL_LANG = 'original'

let names: Intl.DisplayNames | null = null
try {
  names = new Intl.DisplayNames(['en'], { type: 'language' })
} catch {
  names = null
}

/** A language code as people read it: `de` → `German`, `pt-br` → `Brazilian Portuguese`. */
export function languageName(code: string): string {
  if (code === ORIGINAL_LANG) return 'Original'
  try {
    const name = names?.of(code)
    return name && name.toLowerCase() !== code.toLowerCase() ? name : code
  } catch {
    return code
  }
}

const storageKey = (slug: string) => `tripl.docs.lang.${slug}`

/** The language this browser last chose for a project's notes, if any. */
export function storedDocLanguage(slug: string): string | null {
  try {
    return window.localStorage.getItem(storageKey(slug))
  } catch {
    return null
  }
}

export function storeDocLanguage(slug: string, lang: string): void {
  try {
    window.localStorage.setItem(storageKey(slug), lang)
  } catch {
    // Private mode or storage off: the choice lasts for the page only.
  }
}
