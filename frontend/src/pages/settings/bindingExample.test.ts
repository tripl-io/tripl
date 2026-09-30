import { describe, expect, it } from 'vitest'

import type { Variable } from '@/types'

import { bindingExample } from './bindingExample'

function variable(overrides: Partial<Variable> & { id: string; name: string }): Variable {
  return {
    project_id: 'project-1',
    source_name: null,
    variable_type: 'string',
    allowed_values: [],
    bindings: [],
    description: '',
    ...overrides,
  }
}

describe('bindingExample', () => {
  it('falls back to the generic example when nothing in the project is bound', () => {
    const example = bindingExample([variable({ id: 'v1', name: 'variant' })])

    expect(example).toEqual({
      binding: 'page_data.extra.variant',
      name: 'variant',
      fromProject: false,
    })
  })

  it('prefers a property whose name differs from its binding', () => {
    // The instructive case, and the one this exists for: it shows both roles at
    // once — the scan reads `property.bite_threshold`, the plan writes
    // `${bite_threshold}`. A variable whose name IS its binding shows one thing
    // twice and answers nothing.
    const example = bindingExample([
      variable({ id: 'v1', name: 'page_data.extra.mode', bindings: ['page_data.extra.mode'] }),
      variable({ id: 'v2', name: 'bite_threshold', bindings: ['property.bite_threshold'] }),
    ])

    expect(example).toEqual({
      binding: 'property.bite_threshold',
      name: 'bite_threshold',
      fromProject: true,
    })
  })

  it('uses a name-equals-binding property only when there is nothing else', () => {
    const example = bindingExample([
      variable({ id: 'v1', name: 'page_data.extra.mode', bindings: ['page_data.extra.mode'] }),
    ])

    expect(example.binding).toBe('page_data.extra.mode')
    expect(example.fromProject).toBe(true)
  })

  it('ignores a binding with no dot, which cannot show the distinction', () => {
    // A column named exactly like its variable is the case where no binding is
    // needed at all, so it teaches the reader nothing about the two namespaces.
    const example = bindingExample([variable({ id: 'v1', name: 'action', bindings: ['action'] })])

    expect(example.fromProject).toBe(false)
  })

  it('is deterministic: shortest binding, then alphabetical', () => {
    const variables = [
      variable({ id: 'v1', name: 'zeta', bindings: ['property.zeta_value'] }),
      variable({ id: 'v2', name: 'alpha', bindings: ['property.alpha'] }),
      variable({ id: 'v3', name: 'omega', bindings: ['property.omega'] }),
    ]

    // `property.alpha` and `property.omega` are the same length; alpha wins.
    expect(bindingExample(variables).binding).toBe('property.alpha')
    // …and reversing the input does not change the answer.
    expect(bindingExample([...variables].reverse()).binding).toBe('property.alpha')
  })
})
