import { describe, expect, it } from 'vitest'

import { ApiError } from '@/api/client'

import { splitApiFieldErrors } from './fieldErrors'

function validationError(detail: { loc: (string | number)[]; msg: string }[]) {
  const error = new ApiError('flattened', 422)
  error.fields = detail.map(item => ({ ...item, type: 'value_error' }))
  return error
}

describe('splitApiFieldErrors', () => {
  it('puts a message beside the input it names, without Pydantic\'s prefix', () => {
    const split = splitApiFieldErrors(
      validationError([{ loc: ['body', 'chat_id'], msg: 'Value error, Telegram chat_id is required' }]),
      ['chat_id', 'name'],
    )

    expect(split.fields).toEqual({ chat_id: 'Telegram chat_id is required' })
    expect(split.message).toBeNull()
  })

  it('reads a nested location by its first segment', () => {
    const split = splitApiFieldErrors(
      validationError([{ loc: ['body', 'filters', 0], msg: 'Value error, Filter must have at least one value' }]),
      ['filters'],
    )

    expect(split.fields.filters).toBe('Filter must have at least one value')
  })

  it('names a field the form has no input for, in words', () => {
    const split = splitApiFieldErrors(
      validationError([{ loc: ['body', 'notify_on_drop'], msg: 'Value error, bad' }]),
      ['name'],
      { notify_on_drop: 'Drops' },
    )

    expect(split.fields).toEqual({})
    expect(split.message).toBe('Drops: bad')
  })

  it('keeps a model-level message whole', () => {
    const split = splitApiFieldErrors(
      validationError([{ loc: ['body'], msg: 'Value error, Webhook header name and value must be provided together' }]),
      ['name'],
    )

    expect(split.message).toBe('Webhook header name and value must be provided together')
  })

  it('passes any other error through as one message, prefix stripped', () => {
    expect(splitApiFieldErrors(new Error('Value error, nope'), ['name'])).toEqual({
      fields: {},
      message: 'nope',
    })
    expect(splitApiFieldErrors(null, ['name'])).toEqual({ fields: {}, message: null })
  })
})
