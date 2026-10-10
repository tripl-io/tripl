/// <reference types="node" />
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

import type { AuditActionCatalog } from '@/types'
import { actionOptionLabels, actionSentence, hasWrittenSentence, TARGET_NOUN } from './auditSentences'

/**
 * Every action the backend records, as the Action filter offers it and a row
 * chip reads it.
 *
 * The fixture is a copy of `GET /audit/actions`; the backend's
 * test_prelaunch_plan_settings.py fails when it drifts from
 * `audit_actions.action_catalog()`. Before this, a third of the options were
 * humanised codes: "Values clear variable", "Note alert inbox", "Applied scan
 * config.event groups", "Changed the default docs languages of", and
 * "Updated event (event.update)" wherever two codes read alike.
 */
const HERE = dirname(fileURLToPath(import.meta.url))
const CATALOG = JSON.parse(
  readFileSync(join(HERE, 'auditActionCatalog.fixture.json'), 'utf8'),
) as AuditActionCatalog

const PROJECT_ACTIONS = CATALOG.project.flatMap((group) => group.actions)
// The organization-wide feed offers both halves in one select.
const ALL_ACTIONS = [...PROJECT_ACTIONS, ...CATALOG.workspace.flatMap((group) => group.actions)]

describe('the audit action catalog reads as sentences', () => {
  it('was found and holds both halves', () => {
    // Guard the guard: an empty fixture would pass every check below.
    expect(PROJECT_ACTIONS.length).toBeGreaterThan(50)
    expect(CATALOG.workspace.length).toBeGreaterThan(0)
  })

  it('gives every recorded action a written sentence or a known verb', () => {
    const humanised = ALL_ACTIONS.filter((action) => !hasWrittenSentence(action))
    expect(humanised).toEqual([])
  })

  it('never leaks a code into a label', () => {
    const leaky = ALL_ACTIONS.map(actionSentence).filter((label) => /[._()]/.test(label))
    expect(leaky).toEqual([])
  })

  it('ends every label on a word, not a dangling preposition', () => {
    // The option has no target after it, so "Changed the default docs
    // languages of" read as a cut-off sentence.
    const dangling = ALL_ACTIONS.map(actionSentence).filter((label) =>
      /\s(of|to|from|for|on|in|into|with|by)$/.test(label),
    )
    expect(dangling).toEqual([])
  })

  it('offers no two options that read alike, so none needs its code in brackets', () => {
    for (const actions of [PROJECT_ACTIONS, ALL_ACTIONS]) {
      const labels = [...actionOptionLabels(actions).values()]
      expect(labels.filter((label) => label.includes('('))).toEqual([])
      expect(new Set(labels).size).toBe(actions.length)
    }
  })

  it('calls a variable a property and an alert inbox entry an incident', () => {
    expect(actionSentence('variable.create')).toBe('Created property')
    expect(actionSentence('variable.values_clear')).toBe('Cleared the values of property')
    expect(actionSentence('alert_inbox.note')).toBe('Added a note to incident')
    expect(TARGET_NOUN.variable).toBe('property')
    const propertyGroup = CATALOG.project.find((group) =>
      group.actions.every((action) => action.startsWith('variable.')),
    )
    expect(propertyGroup?.label).toBe('Properties')
  })

  it('reads the actions the screenshot walk caught as sentences', () => {
    expect(actionSentence('scan_config.run')).toBe('Ran scan')
    expect(actionSentence('scan_config.event_groups.apply')).toBe('Applied event groups from scan')
    expect(actionSentence('project.docs_languages')).toBe('Changed the default docs languages of project')
    expect(actionSentence('signal.unacknowledge')).toBe('Unacknowledged signal')
    expect(actionSentence('event_photo.figma_attach')).toBe('Attached a Figma frame to an event')
    expect(actionSentence('event.bulk_update')).toBe('Updated events in bulk')
  })

  it('humanises a nested code without leaking its dot', () => {
    expect(actionSentence('widget.part_x.frob')).toBe('Frob widget part x')
  })
})
