/**
 * The anchor of a term's glossary row on the Concepts page, `term-<id>`, so
 * other pages can deep-link `concepts#term-metric-points` (#238 /
 * JR-32). Here rather than in ConceptsPage so a page linking to a term does not
 * pull the whole glossary page into its chunk.
 */
export function termAnchor(term: string): string {
  return `term-${term.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')}`
}
