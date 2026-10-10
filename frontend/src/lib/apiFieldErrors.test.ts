import { describe, expect, it } from 'vitest'
import { ApiError, type ApiFieldError } from '@/api/client'
import { splitApiFieldErrors } from './apiFieldErrors'

function validationError(fields: ApiFieldError[]): ApiError {
  const error = new ApiError('flattened', 422)
  error.fields = fields
  return error
}

describe('splitApiFieldErrors', () => {
  it('puts a message beside the input its field names', () => {
    const split = splitApiFieldErrors(
      validationError([
        { loc: ['body', 'new_password'], msg: 'Password must include a number.', type: 'value_error' },
      ]),
      ['new_password'],
    )
    expect(split).toEqual({
      fields: { new_password: 'Password must include a number.' },
      message: null,
    })
  })

  it('joins two messages for one input rather than dropping one', () => {
    const split = splitApiFieldErrors(
      validationError([
        { loc: ['body', 'name'], msg: 'Too long.', type: 'string_too_long' },
        { loc: ['body', 'name'], msg: 'Not allowed.', type: 'value_error' },
      ]),
      ['name'],
    )
    expect(split.fields).toEqual({ name: 'Too long. Not allowed.' })
  })

  it('labels a field the form has no input for, and keeps a model-level sentence whole', () => {
    const split = splitApiFieldErrors(
      validationError([
        { loc: ['body', 'org_slug'], msg: 'is reserved', type: 'value_error' },
        { loc: ['body'], msg: 'org_name and org_slug are required', type: 'value_error' },
      ]),
      ['email'],
      { org_slug: 'Organization URL slug' },
    )
    expect(split).toEqual({
      fields: {},
      message: 'Organization URL slug: is reserved org_name and org_slug are required',
    })
  })

  it('falls back to a readable name for an unlabelled field', () => {
    const split = splitApiFieldErrors(
      validationError([{ loc: ['query', 'page_size'], msg: 'too big', type: 'less_than_equal' }]),
      [],
    )
    expect(split.message).toBe('page size: too big')
  })

  it('keeps any other error as the message alone', () => {
    expect(splitApiFieldErrors(new ApiError('Invalid email or password', 401), ['email'])).toEqual({
      fields: {},
      message: 'Invalid email or password',
    })
    expect(splitApiFieldErrors(null, ['email'])).toEqual({ fields: {}, message: null })
  })
})
