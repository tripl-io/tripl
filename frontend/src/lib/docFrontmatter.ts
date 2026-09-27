/**
 * Split a note into its YAML frontmatter block and Markdown body, the way
 * `services/docs_frontmatter.split_frontmatter` does: the block starts at
 * byte 0 with `---` on its own line and closes with `---` or `...` on its own
 * line. The editor preview renders the body; the server parses the YAML.
 */
export function splitFrontmatter(content: string): { frontmatter: string | null; body: string } {
  const text = content.replace(/\r\n/g, '\n')
  if (!text.startsWith('---\n')) return { frontmatter: null, body: content }
  const rest = text.slice(4)
  const close = /^(?:---|\.\.\.)[ \t]*(?:\n|$)/m.exec(rest)
  if (!close) return { frontmatter: null, body: content }
  return {
    frontmatter: rest.slice(0, close.index),
    body: rest.slice(close.index + close[0].length),
  }
}
