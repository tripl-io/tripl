import { describe, expect, it } from 'vitest'

import type { AlertRule } from '@/types'

import {
  FILTER_OPERATOR_OPTIONS,
  ITEM_TEMPLATE_VARIABLE_OPTIONS,
  TEMPLATE_VARIABLE_OPTIONS,
  defaultRuleForm,
  formatCooldownLong,
  joinCooldown,
  ruleConditionSummary,
  ruleDraftSummary,
  splitCooldown,
  findTemplateVariableToken,
  getDefaultItemsTemplate,
  getDefaultMessageTemplate,
  hasRuleFormProblems,
  messageFormatForDestination,
  ruleFormProblems,
  ruleFormToPayload,
  ruleSignalLabels,
  ruleToForm,
  unknownTemplateVariables,
  withMessageFormat,
  type RuleFormState,
} from './constants'

function makeRule(overrides: Partial<AlertRule> = {}): AlertRule {
  return {
    id: 'rule-1',
    destination_id: 'dest-1',
    scan_config_id: 'scan-1',
    name: 'Checkout drops',
    enabled: true,
    include_project_total: true,
    include_event_types: false,
    include_events: true,
    include_schema_drifts: false,
    include_distribution_drifts: false,
    include_release_regressions: false,
    include_variable_value_drifts: false,
    include_metrics: false,
    include_source_freshness: false,
    notify_on_spike: false,
    notify_on_drop: true,
    ai_explanation_enabled: false,
    min_percent_delta: 37.5,
    min_absolute_delta: 2,
    min_expected_count: 10,
    cooldown_minutes: 90,
    message_template: null,
    items_template: null,
    message_format: 'plain',
    filters: [{ id: 'f-1', field: 'direction', operator: 'eq', values: ['down'] }],
    muted: false,
    muted_until: null,
    total_deliveries: 0,
    incident_count: 0,
    last_delivery_at: null,
    last_delivery_status: null,
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    ...overrides,
  } as AlertRule
}

function form(overrides: Partial<RuleFormState> = {}): RuleFormState {
  return { ...defaultRuleForm(), name: 'Rule', ...overrides }
}

describe('ruleToForm → ruleFormToPayload (ALR-43)', () => {
  it('round-trips a saved rule to the same settings on the wire', () => {
    const payload = ruleFormToPayload(ruleToForm(makeRule()))

    expect(payload).toMatchObject({
      name: 'Checkout drops',
      scan_config_id: 'scan-1',
      min_percent_delta: 37.5,
      min_absolute_delta: 2,
      min_expected_count: 10,
      cooldown_minutes: 90,
      notify_on_spike: false,
      notify_on_drop: true,
      message_format: 'plain',
      filters: [{ field: 'direction', operator: 'eq', values: ['down'] }],
    })
  })

  it('sends numbers, not the text the inputs hold (ALR-16)', () => {
    const payload = ruleFormToPayload(form({ cooldown_minutes: ' 45 ', min_percent_delta: '12.5' }))

    expect(payload.cooldown_minutes).toBe(45)
    expect(payload.min_percent_delta).toBe(12.5)
  })

  it('nulls templates that are still the defaults, and keeps an edited one', () => {
    expect(ruleFormToPayload(form()).message_template).toBeNull()
    expect(ruleFormToPayload(form()).items_template).toBeNull()
    expect(ruleFormToPayload(form({ message_template: 'Hi ${rule_name}' })).message_template)
      .toBe('Hi ${rule_name}')
  })

  it('sends an explicit null for "all scans"', () => {
    expect(ruleFormToPayload(form({ scan_config_id: '' }))).toHaveProperty('scan_config_id', null)
  })

  it('keeps a filter row with no values on the wire instead of silently widening the rule (ALR-5)', () => {
    const payload = ruleFormToPayload(
      form({ filters: [{ uid: 'f-1', field: 'event_type', operator: 'in', values: [] }] }),
    )

    expect(payload.filters).toEqual([{ field: 'event_type', operator: 'in', values: [] }])
  })

  it('sends one value for a single-value operator', () => {
    const payload = ruleFormToPayload(
      form({ filters: [{ uid: 'f-1', field: 'direction', operator: 'eq', values: ['up', 'down'] }] }),
    )

    expect(payload.filters).toEqual([{ field: 'direction', operator: 'eq', values: ['up'] }])
  })
})

describe('ruleFormProblems', () => {
  it('finds nothing wrong with the default form', () => {
    expect(hasRuleFormProblems(ruleFormProblems(form()))).toBe(false)
  })

  it('refuses an emptied or zero cooldown, and a fractional one (ALR-16)', () => {
    expect(ruleFormProblems(form({ cooldown_minutes: '' })).numeric.cooldown_minutes).toMatch(/number/)
    expect(ruleFormProblems(form({ cooldown_minutes: '0' })).numeric.cooldown_minutes).toMatch(/1 or more/)
    expect(ruleFormProblems(form({ cooldown_minutes: '1.5' })).numeric.cooldown_minutes).toMatch(/whole/)
  })

  it('refuses a negative or emptied threshold but accepts 0', () => {
    expect(ruleFormProblems(form({ min_percent_delta: '-1' })).numeric.min_percent_delta).toBeDefined()
    expect(ruleFormProblems(form({ min_absolute_delta: '' })).numeric.min_absolute_delta).toBeDefined()
    expect(ruleFormProblems(form({ min_expected_count: '0' })).numeric.min_expected_count).toBeUndefined()
  })

  it('names a filter row with no values by its uid (ALR-5)', () => {
    const problems = ruleFormProblems(
      form({ filters: [{ uid: 'f-9', field: 'event_type', operator: 'in', values: [] }] }),
    )

    expect(problems.filters['f-9']).toMatch(/at least one value/)
    expect(hasRuleFormProblems(problems)).toBe(true)
  })

  it('refuses a rule with no direction (ALR-14) or no scope (ALR-15)', () => {
    expect(ruleFormProblems(form({ notify_on_spike: false, notify_on_drop: false })).direction)
      .toMatch(/direction/)
    expect(
      ruleFormProblems(form({
        include_project_total: false,
        include_event_types: false,
        include_events: false,
      })).scopes,
    ).toMatch(/signal kind/)
  })
})

describe('message format on a destination switch (ALR-4)', () => {
  it('keeps a format the new channel supports', () => {
    expect(messageFormatForDestination('plain', 'telegram')).toBe('plain')
    expect(messageFormatForDestination('telegram_html', 'telegram')).toBe('telegram_html')
  })

  it('falls back to plain text for one it does not', () => {
    expect(messageFormatForDestination('slack_mrkdwn', 'telegram')).toBe('plain')
    expect(messageFormatForDestination('telegram_html', 'webhook')).toBe('plain')
  })

  it('carries default templates to the new format and leaves an edited one alone', () => {
    const untouched = withMessageFormat(
      form({
        message_format: 'slack_mrkdwn',
        message_template: getDefaultMessageTemplate('slack_mrkdwn'),
        items_template: getDefaultItemsTemplate('slack_mrkdwn'),
      }),
      'plain',
    )
    expect(untouched.message_template).toBe(getDefaultMessageTemplate('plain'))
    expect(untouched.items_template).toBe(getDefaultItemsTemplate('plain'))

    const edited = withMessageFormat(
      form({ message_format: 'slack_mrkdwn', message_template: '*mine*' }),
      'plain',
    )
    expect(edited.message_format).toBe('plain')
    expect(edited.message_template).toBe('*mine*')
  })
})

describe('unknownTemplateVariables (ALR-21)', () => {
  it('names a typo once, in the order it appears', () => {
    expect(
      unknownTemplateVariables('${scope_nme} ${rule_name} ${scope_nme} ${oops}', TEMPLATE_VARIABLE_OPTIONS),
    ).toEqual(['scope_nme', 'oops'])
  })

  it('flags an item variable used in the message template, and accepts it in the items one', () => {
    expect(unknownTemplateVariables('${scope_name}', TEMPLATE_VARIABLE_OPTIONS)).toEqual(['scope_name'])
    expect(unknownTemplateVariables('${scope_name}', ITEM_TEMPLATE_VARIABLE_OPTIONS)).toEqual([])
  })

  it('finds nothing wrong with either default template', () => {
    for (const format of ['plain', 'slack_mrkdwn', 'telegram_html', 'telegram_markdownv2'] as const) {
      expect(unknownTemplateVariables(getDefaultMessageTemplate(format), TEMPLATE_VARIABLE_OPTIONS)).toEqual([])
      expect(unknownTemplateVariables(getDefaultItemsTemplate(format), ITEM_TEMPLATE_VARIABLE_OPTIONS)).toEqual([])
    }
  })
})

describe('findTemplateVariableToken', () => {
  it('reads the half-typed variable before the cursor', () => {
    expect(findTemplateVariableToken('Hi ${rule_', 10)).toEqual({ start: 3, end: 10, query: 'rule_' })
  })

  it('ignores a closed variable and plain text', () => {
    expect(findTemplateVariableToken('${rule_name} x', 14)).toBeNull()
    expect(findTemplateVariableToken('no token', 8)).toBeNull()
  })
})

describe('defaultRuleForm — a drop rule that can fire before zero (AL-2)', () => {
  it('starts at 30%, not the 100% a drop can only reach at zero volume', () => {
    expect(defaultRuleForm().min_percent_delta).toBe('30')
  })
})

describe('cooldown units (AL-6)', () => {
  it.each([
    ['1440', { amount: '1', unit: 'days' }],
    ['360', { amount: '6', unit: 'hours' }],
    ['45', { amount: '45', unit: 'minutes' }],
  ] as const)('shows %s minutes in the largest whole unit', (minutes, expected) => {
    expect(splitCooldown(minutes)).toEqual(expected)
  })

  it.each(['', '0', ' 45 ', '1.5', 'abc', '007'])('round-trips %j untouched', (text) => {
    const { amount, unit } = splitCooldown(text)
    expect(joinCooldown(amount, unit)).toBe(text)
  })

  it('converts an amount in hours or days back to minutes', () => {
    expect(joinCooldown('2', 'days')).toBe('2880')
    expect(joinCooldown('1.5', 'hours')).toBe('90')
    expect(joinCooldown('', 'hours')).toBe('')
  })

  it('says the cooldown in words for a sentence', () => {
    expect(formatCooldownLong(1440)).toBe('1 day')
    expect(formatCooldownLong(360)).toBe('6 hours')
    expect(formatCooldownLong(45)).toBe('45 minutes')
  })
})

describe('filter operators read as words (AL-39)', () => {
  it('has no SQL-speak', () => {
    expect(FILTER_OPERATOR_OPTIONS.map(option => option.label))
      .toEqual(['is', 'is not', 'is one of', 'is not one of'])
  })
})

describe('ruleConditionSummary (AL-11, JR-15)', () => {
  it('writes the condition as a sentence and lists what the rule watches', () => {
    const summary = ruleConditionSummary(makeRule({
      notify_on_spike: true,
      notify_on_drop: true,
      min_percent_delta: 100,
      cooldown_minutes: 1440,
    }))
    expect(summary.condition).toBe('Spikes & drops ≥ 100% · 1d cooldown')
  })

  it('tells a metrics-only rule apart from the others', () => {
    const summary = ruleConditionSummary(makeRule({
      include_project_total: false,
      include_event_types: false,
      include_events: false,
      include_metrics: true,
      filters: [],
    }))
    expect(summary.watches).toBe('Metrics')
  })
})

describe('ruleDraftSummary (AL-1)', () => {
  it('says what Create will set up', () => {
    const form: RuleFormState = { ...defaultRuleForm(), name: 'x' }
    expect(ruleDraftSummary(form, '#alerts')).toBe(
      'Sends to #alerts when any of project total, event types, events spikes or drops by at least 30%, then waits 1 day before alerting on the same scope again.',
    )
  })

  it('says nothing before a destination is picked', () => {
    expect(ruleDraftSummary(defaultRuleForm(), null)).toBeNull()
  })
})

describe('source freshness scope (F16, #269)', () => {
  it('is off on a new rule and carried both ways', () => {
    expect(defaultRuleForm().include_source_freshness).toBe(false)
    expect(ruleFormToPayload(ruleToForm(makeRule({ include_source_freshness: true }))))
      .toMatchObject({ include_source_freshness: true })
  })

  it('reads a rule from a server that predates the flag as off', () => {
    const legacy = makeRule()
    delete (legacy as Partial<AlertRule>).include_source_freshness
    expect(ruleToForm(legacy).include_source_freshness).toBe(false)
  })

  it('counts as a signal kind on its own', () => {
    const only = form({
      include_project_total: false,
      include_event_types: false,
      include_events: false,
      include_source_freshness: true,
    })
    expect(ruleFormProblems(only).scopes).toBeNull()
    expect(ruleSignalLabels(only).drift).toEqual(['Source freshness'])
  })
})
