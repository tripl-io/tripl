// Cyrillic → Latin, so a Russian display name still yields a readable
// identifier; every other script falls through to the generic fallback.
const CYRILLIC: Record<string, string> = {
  а: 'a', б: 'b', в: 'v', г: 'g', д: 'd', е: 'e', ё: 'e', ж: 'zh', з: 'z', и: 'i',
  й: 'y', к: 'k', л: 'l', м: 'm', н: 'n', о: 'o', п: 'p', р: 'r', с: 's', т: 't',
  у: 'u', ф: 'f', х: 'kh', ц: 'ts', ч: 'ch', ш: 'sh', щ: 'shch', ъ: '', ы: 'y',
  ь: '', э: 'e', ю: 'yu', я: 'ya', і: 'i', ї: 'yi', є: 'ye', ґ: 'g',
}

/**
 * Derive a snake_case identifier from a display name: transliterated and
 * stripped of accents, lower-cased, runs of anything else collapsed to one
 * underscore, trimmed. A name with nothing Latin left in it (Chinese, Arabic…)
 * yields `fallback` instead of the empty string it used to, which left the
 * required internal-name field blank until submit. Shared by the
 * metric and fact-table forms, which both pre-fill an internal name this way.
 */
export function toIdentifier(input: string, fallback: string): string {
  const latin = input
    .toLowerCase()
    .replace(/[Ѐ-ӿ]/g, char => CYRILLIC[char] ?? '')
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
  const snake = latin.replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '')
  if (snake) return snake
  return input.trim() ? fallback : ''
}
