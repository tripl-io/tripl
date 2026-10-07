/// <reference types="node" />
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join, relative, resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * Every coach mark sits on something that can take its ref.
 *
 * `ScenarioCoachMark` clones a ref onto its single child and coaches only once
 * that ref reports a laid-out box. A child component that drops the ref leaves
 * the mark measuring nothing: no ring, no tag, and the demo guide tells the user
 * "the highlighted control isn't visible" while they are looking at it. Two
 * chapters shipped that way — the branch comment thread and the rule actions
 * menu — and a newcomer following the guide could not finish either.
 *
 * So the direct child must be a DOM element, or a component known to put its
 * ref on a DOM node.
 */

const SRC = resolve(dirname(fileURLToPath(import.meta.url)), '..')

// Components that pass `ref` through to a DOM node (React 19: `ref` is a prop,
// and each of these spreads its props onto the element or a Radix primitive).
const REF_FORWARDING = new Set(['Button', 'IconButton', 'Link', 'TableRow', 'DropdownMenuTrigger'])

function tsxFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) return tsxFiles(path)
    return path.endsWith('.tsx') && !path.includes('.test.') ? [path] : []
  })
}

interface CoachChild {
  where: string
  /** The child's JSX tag, or `null` when the child is a `{…}` expression. */
  tag: string | null
  /** The start of a `{…}` child, to tell a plain element variable from a conditional. */
  expression: string
}

function coachChildren(): CoachChild[] {
  const found: CoachChild[] = []
  for (const file of tsxFiles(SRC)) {
    const source = readFileSync(file, 'utf8')
    for (const match of source.matchAll(/<ScenarioCoachMark\b[^>]*?>/gs)) {
      const step = /step=["{]([^"}]+)/.exec(match[0])?.[1] ?? '?'
      const rest = source
        .slice((match.index ?? 0) + match[0].length)
        .replace(/^\s*(\{\/\*[\s\S]*?\*\/\}\s*)*/, '')
      found.push({
        where: `${relative(SRC, file)} ${step}`,
        tag: /^<([A-Za-z][\w.]*)/.exec(rest)?.[1] ?? null,
        expression: rest.startsWith('{') ? (rest.split('\n')[0] ?? '') : '',
      })
    }
  }
  return found
}

describe('coach marks', () => {
  it('finds the marks it is meant to check', () => {
    expect(coachChildren().length).toBeGreaterThan(10)
  })

  it('anchor on a DOM element or a ref-forwarding component', () => {
    const unanchored = coachChildren()
      .filter(({ tag }) => tag !== null && /^[A-Z]/.test(tag) && !REF_FORWARDING.has(tag))
      .map(({ where, tag }) => `${where} <${tag}>`)
    expect(unanchored).toEqual([])
  })

  it('wrap a conditional child in a DOM element', () => {
    // `{cond ? <A/> : <B/>}` straight under a mark hides which element the ref
    // lands on, and one of the two branches was a component that dropped it.
    const conditional = coachChildren()
      .filter(({ expression }) => /^\{\s*[\w.!]+\s*\?/.test(expression))
      .map(({ where }) => where)
    expect(conditional).toEqual([])
  })
})
