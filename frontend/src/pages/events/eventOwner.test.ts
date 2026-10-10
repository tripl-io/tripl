import { describe, expect, it } from 'vitest'

import { ownerFieldHint, ownerNames, ownersOfType, typeOwnerSource } from './eventOwner'

const AUDIT = { event_type_id: 'type-1', user_name: 'Audit Owner', user_email: 'audit@example.com' }
const JANE = { event_type_id: 'type-1', user_name: '', user_email: 'jane@example.com' }
const OTHER = { event_type_id: 'type-2', user_name: 'Someone Else', user_email: 'else@example.com' }

describe('ownersOfType', () => {
  it("keeps one type's owners, in the order the list gives them", () => {
    expect(ownersOfType([AUDIT, OTHER, JANE], 'type-1')).toEqual([AUDIT, JANE])
    expect(ownersOfType([AUDIT], 'type-3')).toEqual([])
  })
})

describe('ownerNames', () => {
  it('names each owner, by email when they have no name', () => {
    expect(ownerNames([AUDIT, JANE])).toBe('Audit Owner, jane@example.com')
  })
})

describe('typeOwnerSource', () => {
  it('agrees with the count', () => {
    expect(typeOwnerSource(1)).toBe('event type owner')
    expect(typeOwnerSource(2)).toBe('event type owners')
  })
})

describe('ownerFieldHint', () => {
  it("says who answers when no owner is picked and the type has owners", () => {
    expect(ownerFieldHint('', 'Screen View', [AUDIT])).toBe(
      "Who answers for this event. Without one, the Screen View event type's owners do: Audit Owner.",
    )
    expect(ownerFieldHint('', undefined, [AUDIT, JANE], { plural: true })).toBe(
      "Who answers for these events. Without one, the event type's owners do: Audit Owner, jane@example.com.",
    )
  })

  it('keeps the plain hint once an owner is picked, or when the type has none', () => {
    expect(ownerFieldHint('u-1', 'Screen View', [AUDIT])).toBe('Who answers for this event.')
    expect(ownerFieldHint('', 'Screen View', [])).toBe('Who answers for this event.')
    expect(ownerFieldHint('', 'Screen View', undefined)).toBe('Who answers for this event.')
  })
})
