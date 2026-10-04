import { describe, expect, it } from 'vitest'
import { parseContract, parseDecimal, regexNotice, validateContract, type ContractDraft } from './fieldContract'

const DRAFT: ContractDraft = {
  contract_max_bad_rate: '0',
  contract_required_max_null_rate: '',
  contract_regex: '',
  contract_min_value: '',
  contract_max_value: '',
}

describe('fieldContract', () => {
  it('reads a decimal comma as a point, but not a thousands separator', () => {
    expect(parseDecimal('0,5')).toBe(0.5)
    expect(parseDecimal('1,000')).toBeUndefined()
    expect(parseDecimal('abc')).toBeUndefined()
    expect(parseDecimal('')).toBeUndefined()
  })

  it('accepts the defaults and turns blank optional rules into null', () => {
    expect(validateContract(DRAFT)).toEqual({})
    expect(parseContract(DRAFT)).toEqual({
      contract_max_bad_rate: 0,
      contract_required_max_null_rate: null,
      contract_regex: null,
      contract_min_value: null,
      contract_max_value: null,
    })
  })

  it('names every problem instead of dropping or tightening the rule', () => {
    const errors = validateContract({
      contract_max_bad_rate: '',
      contract_required_max_null_rate: '1.5',
      contract_regex: '(',
      contract_min_value: '5',
      contract_max_value: '2',
    })
    expect(Object.keys(errors).sort()).toEqual([
      'contract_max_bad_rate',
      'contract_max_value',
      'contract_required_max_null_rate',
    ])
    expect(errors.contract_max_value).toBe('Max must be at least Min.')
  })
})

describe('fieldContract regex', () => {
  it('never blocks a pattern JavaScript cannot compile: Python and RE2 accept these', () => {
    for (const pattern of ['(?i)^checkout_', '(?P<name>a)', '(']) {
      expect(validateContract({ ...DRAFT, contract_regex: pattern })).toEqual({})
    }
    // A note, not an error.
    expect(regexNotice('(?i)^checkout_')).toMatch(/server checks it/)
    expect(regexNotice('^[a-z]+$')).toBeUndefined()
  })
})
