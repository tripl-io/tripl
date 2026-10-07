/// <reference types="node" />
import { readdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join, relative } from 'node:path'
import ts from 'typescript'
import { describe, expect, it } from 'vitest'

/**
 * Keeps margin resets off the children of a `space-y-*` / `space-x-*` parent.
 *
 * Tailwind v4 emits the space utilities as
 * `:where(.space-y-4 > :not(:last-child)) { margin-block-end: … }` — zero
 * specificity — so `m-0` on a direct child beats it and the gap is gone. The
 * reset does nothing else: preflight already zeroes every element's margin.
 * That is how the empty workspace's welcome came out with its heading, both
 * paragraphs and every card's text flush against each other.
 *
 * Checked on the JSX a file shows: an element's parent is the element it sits
 * in, through fragments and `.map` callbacks. A prop value or a render
 * callback's result renders somewhere this file does not show, and is not
 * checked.
 */

const SELF = fileURLToPath(import.meta.url)
const SRC = dirname(SELF)

type Axis = 'x' | 'y'
type JsxNode = ts.JsxElement | ts.JsxSelfClosingElement

const SPACE = /(?:^|[\s:"'`])space-([xy])-(?!reverse)/g
const RESETS: Record<Axis, readonly string[]> = {
  y: ['m-0', 'mb-0', 'my-0'],
  x: ['m-0', 'me-0', 'mr-0', 'mx-0'],
}
/** Components whose root spaces its children whatever the call site passes. */
const BUILT_IN_SPACE: Record<string, Axis> = { PageContainer: 'y' }

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) return sourceFiles(full)
    return entry.name.endsWith('.tsx') && !entry.name.includes('.test.') ? [full] : []
  })
}

function tagOf(node: JsxNode): string {
  return (ts.isJsxElement(node) ? node.openingElement.tagName : node.tagName).getText()
}

function classNameOf(node: JsxNode): ts.JsxAttribute | undefined {
  const attributes = ts.isJsxElement(node) ? node.openingElement.attributes : node.attributes
  return attributes.properties.find(
    (property): property is ts.JsxAttribute =>
      ts.isJsxAttribute(property) && property.name.getText() === 'className',
  )
}

/** The text of every string literal inside `node`, at any depth. */
function literalTexts(node: ts.Node | undefined, out: string[] = []): string[] {
  if (!node) return out
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) out.push(node.text)
  else if (ts.isTemplateExpression(node)) {
    out.push(node.head.text, ...node.templateSpans.map((span) => span.literal.text))
    node.templateSpans.forEach((span) => literalTexts(span.expression, out))
  } else ts.forEachChild(node, (child) => void literalTexts(child, out))
  return out
}

function isMapCallback(fn: ts.Node): boolean {
  const call = fn.parent
  return (
    ts.isCallExpression(call) &&
    ts.isPropertyAccessExpression(call.expression) &&
    ['map', 'flatMap'].includes(call.expression.name.getText()) &&
    call.arguments[0] === fn
  )
}

/** The element `node` renders directly inside, when this file shows it. */
function domParent(node: JsxNode): JsxNode | null {
  let current: ts.Node | undefined = node.parent
  while (current) {
    if (ts.isJsxElement(current)) return current
    if (ts.isJsxAttribute(current) || ts.isJsxSpreadAttribute(current)) return null
    if ((ts.isArrowFunction(current) || ts.isFunctionExpression(current)) && !isMapCallback(current)) {
      return null
    }
    if (ts.isFunctionDeclaration(current) || ts.isVariableDeclaration(current)) return null
    current = current.parent
  }
  return null
}

function spaceAxes(node: JsxNode): Set<Axis> {
  const axes = new Set<Axis>()
  const builtIn = BUILT_IN_SPACE[tagOf(node)]
  if (builtIn) axes.add(builtIn)
  for (const text of literalTexts(classNameOf(node)?.initializer)) {
    for (const match of text.matchAll(SPACE)) axes.add(match[1] as Axis)
  }
  return axes
}

/** `line N: <tag reset> in <parent>` for every reset that cancels a parent's spacing. */
function resetsUnderSpace(name: string, text: string): string[] {
  const source = ts.createSourceFile(name, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  const found: string[] = []
  const visit = (node: ts.Node) => {
    if (ts.isJsxElement(node) || ts.isJsxSelfClosingElement(node)) {
      const className = classNameOf(node)
      const parent = className ? domParent(node) : null
      if (className && parent) {
        const resets = new Set([...spaceAxes(parent)].flatMap((axis) => RESETS[axis]))
        const tokens = literalTexts(className.initializer).flatMap((part) => part.split(/\s+/))
        const hit = tokens.filter((token) => resets.has(token))
        if (hit.length > 0) {
          const { line } = source.getLineAndCharacterOfPosition(node.getStart())
          found.push(`line ${line + 1}: <${tagOf(node)} ${hit.join(' ')}> in <${tagOf(parent)}>`)
        }
      }
    }
    ts.forEachChild(node, visit)
  }
  visit(source)
  return found
}

describe('margin resets under space utilities', () => {
  it('are not set on a direct child of a space-y / space-x parent', () => {
    const offenders: Record<string, string[]> = {}
    for (const file of sourceFiles(SRC)) {
      const found = resetsUnderSpace(file, readFileSync(file, 'utf8'))
      if (found.length > 0) offenders[relative(SRC, file)] = found
    }
    expect(
      offenders,
      'Drop the reset (preflight already zeroes margins): on a child of space-y-* / '
        + 'space-x-* it cancels the gap. Or give the parent flex + gap-* instead.',
    ).toEqual({})
  })

  it('finds the resets it guards against, and only those', () => {
    const probe = [
      '<div className="space-y-3">',
      '  <h2 className="m-0 text-title">Title</h2>',
      '  {items.map((item) => <p key={item} className="mb-0">{item}</p>)}',
      '  <Field icon={<span className="m-0" />} />',
      '</div>;',
      '<div className="flex gap-2"><p className="m-0">Gap</p></div>;',
      '<div className="sm:space-x-2"><span className="me-0">A</span><span>B</span></div>;',
      '<PageContainer><p className="my-0">Page</p></PageContainer>;',
    ].join('\n')
    expect(resetsUnderSpace('probe.tsx', probe)).toEqual([
      'line 2: <h2 m-0> in <div>',
      'line 3: <p mb-0> in <div>',
      'line 7: <span me-0> in <div>',
      'line 8: <p my-0> in <PageContainer>',
    ])
  })
})
