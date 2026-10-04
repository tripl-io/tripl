/**
 * The initials a user avatar shows, derived ONE way everywhere.
 *
 * Four copies used to disagree: some took the first letter of the first two
 * words, one took two letters of the first word whatever the name, and one
 * fell back to "?" where the others showed "•". The same account then read
 * "JS" in the sidebar and "JO" on another page.
 *
 * - "John Smith Doe" → "JS" (first letters of the first two words)
 * - "alice" / "alice@example.com" → "AL" (first two letters)
 * - blank → "•"
 */
export function initialsOf(nameOrEmail: string | null | undefined): string {
  const trimmed = (nameOrEmail ?? '').trim()
  if (!trimmed) return '•'
  const words = trimmed.split(/\s+/)
  if (words.length > 1) {
    return words
      .slice(0, 2)
      .map((word) => word.charAt(0).toUpperCase())
      .join('')
  }
  return trimmed.slice(0, 2).toUpperCase()
}
