import type { Variable } from '@/types'

export interface BindingExample {
  /** Where the value lives in the warehouse: a column, or a dotted path inside
   *  one. This is what a data binding holds. */
  binding: string
  /** The variable's name — what goes inside `${…}` in a field value. */
  name: string
  /** False when the project has nothing to draw on and the generic example is
   *  being shown. Callers word the caption differently for the two cases: one
   *  is "here is how yours are wired", the other is "here is the idea". */
  fromProject: boolean
}

/**
 * The generic example, used only when the project has no bound variable to
 * point at. `variant` behind `page_data.extra.variant` is the shape the docs
 * use.
 */
const GENERIC: BindingExample = {
  binding: 'page_data.extra.variant',
  name: 'variant',
  fromProject: false,
}

/**
 * An example of the two namespaces, drawn from the project's own variables.
 *
 * A reader asked whether the dotted path under **Data bindings** and the dotted
 * token offered after `$` in a field value are the same thing. They are not —
 * one is a warehouse address, the other is a variable's name — and the hard-
 * coded example made it worse rather than better: `page_data.extra.variant` is
 * three segments deep in a container their warehouse does not have, so it read
 * as a different namespace from the two-segment names they pick from the token
 * list.
 *
 * They look alike for a reason worth showing rather than explaining: a scan
 * that discovers a path stores it as the binding AND, when every short name is
 * taken, as the name too. So the instructive example is a variable whose name
 * DIFFERS from its binding — it shows both roles at once, in the project's own
 * vocabulary. One where they match teaches nothing, and is preferred only if
 * there is nothing else.
 *
 * Deterministic: ties break on the shortest binding, then alphabetically. An
 * example that changed between renders would read as a list rather than as an
 * example.
 */
export function bindingExample(variables: readonly Variable[]): BindingExample {
  const candidates: BindingExample[] = []
  for (const variable of variables) {
    for (const binding of variable.bindings ?? []) {
      // A binding with no dot is usually the column named exactly like the
      // variable — the case where a binding is not needed at all. It cannot
      // illustrate the distinction, so it is not offered as the example.
      if (!binding.includes('.')) continue
      candidates.push({ binding, name: variable.name, fromProject: true })
    }
  }
  if (candidates.length === 0) return GENERIC
  const differing = candidates.filter(candidate => candidate.binding !== candidate.name)
  const pool = differing.length > 0 ? differing : candidates
  return pool.reduce((best, candidate) => {
    if (candidate.binding.length !== best.binding.length) {
      return candidate.binding.length < best.binding.length ? candidate : best
    }
    return candidate.binding < best.binding ? candidate : best
  })
}
