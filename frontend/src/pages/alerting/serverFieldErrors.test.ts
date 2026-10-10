import { describe, expect, it } from 'vitest'

import { ApiError } from '@/api/client'
import { splitApiFieldErrors } from '@/lib/apiFieldErrors'

/**
 * The alerting dialogs split a 422 with the shared `splitApiFieldErrors`. A
 * rule's filter rows are a list, and the server points at one row: the
 * message still belongs beside the Filters input.
 */
describe('a rule 422 against a filter row', () => {
  it('reads a nested location by its first segment', () => {
    const error = new ApiError('flattened', 422)
    error.fields = [
      { loc: ['body', 'filters', 0], msg: 'Filter must have at least one value', type: 'value_error' },
    ]

    expect(splitApiFieldErrors(error, ['filters', 'name']).fields).toEqual({
      filters: 'Filter must have at least one value',
    })
  })
})
