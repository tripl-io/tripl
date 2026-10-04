/**
 * One definition of a project slug for the two forms that edit one: the create
 * dialog on the workspace page and Project settings › General. The pattern
 * mirrors the backend's `ProjectCreate.slug` / `ProjectUpdate.slug` Field
 * pattern (backend/src/tripl/schemas/project.py); the two used to carry their
 * own copies of it, and only one of them explained it.
 */
export const SLUG_PATTERN = '^[a-z0-9]+(?:-[a-z0-9]+)*$'
export const SLUG_RE = new RegExp(SLUG_PATTERN)

/** What a valid slug looks like, said the same way on both forms. */
export const SLUG_HINT =
  'Used in URLs. Lowercase letters, digits and single hyphens, e.g. mobile-app.'
export const SLUG_ERROR = 'Use only lowercase letters, digits and single hyphens between them.'

export function isValidSlug(value: string): boolean {
  return SLUG_RE.test(value)
}

/**
 * The slug a name folds to, diacritics folded first (`Café Ölmotor` →
 * `cafe-olmotor`); `''` when nothing Latin or numeric is left. The sign-up
 * form derives an organization's slug with it, where a made-up fallback would
 * be worse than an empty field the user fills in.
 */
export function foldSlug(name: string): string {
  return name
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/(^-|-$)/g, '')
}

/**
 * Derive a slug from a project name.
 *
 * Diacritics are folded first (`Café Ölmotor` → `cafe-olmotor`), so a Latin
 * name with accents keeps its words. A name with no Latin letters or digits at
 * all — "Аналитика", "数据" — still folds to nothing, and an empty slug is a
 * required field the browser rejects with its own generic tooltip. Those names
 * get the first `project-<n>` not in `takenSlugs`, which the user can
 * overwrite. It used to be numbered from the project count, which collides as
 * soon as a project is deleted (`project-2`, `project-3` left, count 2 →
 * `project-3` again, and the create fails with a 409).
 */
export function slugify(name: string, takenSlugs: Iterable<string> = []): string {
  const folded = foldSlug(name)
  if (folded) return folded
  if (!name.trim()) return ''
  const taken = new Set(takenSlugs)
  let n = 1
  while (taken.has(`project-${n}`)) n += 1
  return `project-${n}`
}
