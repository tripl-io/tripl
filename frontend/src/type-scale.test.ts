/// <reference types="node" />
import { readdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join, relative } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * Keeps the app on its own type scale.
 *
 * index.css defines the UI text steps (`text-micro`, `text-caption`,
 * `text-body-sm`, `text-body`, `text-heading`) and says to use them, "never
 * `text-[Npx]`, `text-xs` or `text-sm`": Tailwind's own text-sm is 14px against
 * the scale's 13.5 and 12.5, so a page written with it reads a size off the
 * pages around it. The Enterprise package already checked the rule against
 * itself; nothing checked it here. This scans every source file except the
 * tests, which quote the banned sizes to assert they are gone.
 *
 * Comment lines are skipped: a note that names the hand-rolled `text-[10px]`
 * a primitive replaced sizes nothing.
 */

const SRC = dirname(fileURLToPath(import.meta.url))

// `text-sm`, `md:text-xs`, `text-[13px]`; not `text-body-sm` or `text-[var(--danger)]`.
const OFF_SCALE = /\btext-(?:xs|sm)\b|\btext-\[\d[^\]]*\]/g

// A line of a block or JSX comment, or a line comment.
const COMMENT_LINE = /^\s*(?:\*|\/\/|\/\*|\{\/\*)/

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) return sourceFiles(full)
    return /\.tsx?$/.test(entry.name) && !entry.name.includes('.test.') ? [full] : []
  })
}

const offScaleIn = (line: string) => (COMMENT_LINE.test(line) ? [] : line.match(OFF_SCALE) ?? [])

describe('the type scale', () => {
  it('is the only way the app sizes text', () => {
    const offenders: string[] = []
    for (const file of sourceFiles(SRC)) {
      readFileSync(file, 'utf8')
        .split('\n')
        .forEach((line, index) => {
          for (const size of offScaleIn(line)) offenders.push(`${relative(SRC, file)}:${index + 1} ${size}`)
        })
    }
    expect(
      offenders,
      'Use the type scale from index.css instead: text-micro, text-caption, text-body-sm, text-body or text-heading.',
    ).toEqual([])
  })

  it('tells the banned sizes from the scale', () => {
    expect(offScaleIn('className="mt-1 text-sm text-fg-tertiary"')).toEqual(['text-sm'])
    expect(offScaleIn('className="ml-2 md:text-xs"')).toEqual(['text-xs'])
    expect(offScaleIn('className="text-[13px]"')).toEqual(['text-[13px]'])
    expect(offScaleIn('className="text-body-sm text-caption text-[var(--danger)] context-sm"')).toEqual([])
  })

  it('reads a comment that names a banned size as a comment', () => {
    expect(offScaleIn(' * the hand-rolled `rounded border text-[10px]` pills.')).toEqual([])
    expect(offScaleIn('  // was text-sm before the scale')).toEqual([])
    expect(offScaleIn('  {/* text-xs read a size off the table */}')).toEqual([])
  })
})
