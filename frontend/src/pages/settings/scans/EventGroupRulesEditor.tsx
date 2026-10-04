import { useState } from 'react'
import { ChevronDown, ChevronRight, Plus, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { IconButton } from '@/components/ui/icon-button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { NativeSelect } from '@/components/settings/kit'
import type { EventGroupRule, ScanConfigPreview } from '@/types'
import type { UiEventGroupCondition, UiEventGroupRule } from './scanFormTypes'
import { emptyGroupCondition, emptyGroupRule } from './scanFormTypes'

/** Past this many rules the list gets a filter box. */
const FILTER_FROM = 8

const MATCH_OPTIONS = [
  { value: 'all', label: 'All' },
  { value: 'any', label: 'Any' },
]

/** `event_name ~ ^Home$ and screen ~ …` — a rule's conditions on one line. */
function conditionSummary(rule: UiEventGroupRule): string {
  const joiner = rule.condition_logic === 'all' ? ' and ' : ' or '
  return rule.conditions
    .map(condition => `${condition.field || '…'} ~ ${condition.pattern || '…'}`)
    .join(joiner)
}

/**
 * What the save would refuse about this rule, or null. Mirrors the blank-value
 * checks of the backend's EventGroupRule; the regex itself is left to the
 * server, whose dialect (Python `re`) the browser cannot check faithfully.
 */
function ruleProblem(rule: UiEventGroupRule): string | null {
  if (!rule.name.trim()) return 'Needs a group name'
  if (rule.conditions.some(condition => !condition.field.trim() || !condition.pattern.trim())) {
    return 'A condition is incomplete'
  }
  return null
}

export function EventGroupRulesEditor({
  rules,
  columns,
  onChange,
}: {
  rules: UiEventGroupRule[]
  columns?: ScanConfigPreview['columns']
  onChange: (rules: UiEventGroupRule[]) => void
}) {
  // Rules are one line each until opened: the demo's 18 full cards ran the
  // Configuration tab to ~6,000px of identical boxes (#247). Saved rules
  // start closed; a rule added here opens, since it is about to be filled in.
  const [openUids, setOpenUids] = useState<ReadonlySet<string>>(() => new Set())
  const [filter, setFilter] = useState('')
  const toggleOpen = (uid: string) =>
    setOpenUids(current => {
      const next = new Set(current)
      if (next.has(uid)) next.delete(uid)
      else next.add(uid)
      return next
    })
  const addRule = () => {
    const rule = emptyGroupRule()
    setOpenUids(current => new Set(current).add(rule._uid))
    setFilter('')
    onChange([...rules, rule])
  }
  const needle = filter.trim().toLowerCase()
  // An open rule stays listed whatever the filter says: typing its name or
  // regex away from the needle used to unmount the row mid-keystroke. A rule
  // the save would refuse stays listed too, so the pointer to it never hides.
  const visible = rules
    .map((rule, index) => ({ rule, index }))
    .filter(({ rule }) =>
      !needle
      || openUids.has(rule._uid)
      || ruleProblem(rule) != null
      || rule.name.toLowerCase().includes(needle)
      || conditionSummary(rule).toLowerCase().includes(needle))

  const fieldOptions = Array.from(
    new Set([
      'event_name',
      '__event_name',
      ...(columns ?? []).map(column => column.name),
      ...rules.flatMap(rule => rule.conditions.map(condition => condition.field).filter(Boolean)),
    ]),
  )

  const updateRule = (index: number, patch: Partial<UiEventGroupRule>) => {
    onChange(rules.map((rule, ruleIndex) => (
      ruleIndex === index ? { ...rule, ...patch } : rule
    )))
  }

  const updateCondition = (
    ruleIndex: number,
    conditionIndex: number,
    patch: Partial<UiEventGroupCondition>,
  ) => {
    onChange(rules.map((rule, currentRuleIndex) => {
      if (currentRuleIndex !== ruleIndex) return rule
      return {
        ...rule,
        conditions: rule.conditions.map((condition, currentConditionIndex) => (
          currentConditionIndex === conditionIndex ? { ...condition, ...patch } : condition
        )),
      }
    }))
  }

  return (
    <div className="space-y-3 rounded-card border bg-muted/20 p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="text-body font-medium">
            Event groups
            {rules.length > 0 && (
              <>
                {' '}
                <span className="tnum font-normal text-fg-tertiary">· {rules.length}</span>
              </>
            )}
          </div>
          {!columns?.length && (
            <p className="text-body-sm text-fg-tertiary">
              Load a preview to pick real columns; only event_name is available otherwise.
            </p>
          )}
        </div>
        {/* The dry run's flood warning focuses this by id (#247). */}
        <Button id="scan-event-groups" type="button" variant="outline" size="sm" onClick={addRule}>
          <Plus className="size-3.5" aria-hidden="true" />Add group rule
        </Button>
      </div>
      {rules.length > FILTER_FROM && (
        <Input
          type="search"
          aria-label="Filter group rules"
          placeholder="Filter group rules…"
          value={filter}
          onChange={event => setFilter(event.target.value)}
          className="max-w-[280px]"
        />
      )}
      {rules.length === 0 && (
        <p className="text-body-sm text-fg-tertiary">No grouping rules.</p>
      )}
      {rules.length > 0 && visible.length === 0 && (
        <p className="text-body-sm text-fg-tertiary">No group rule matches “{filter}”.</p>
      )}
      {visible.length > 0 && (
        <ul className="m-0 list-none divide-y overflow-hidden rounded-control border bg-background p-0">
          {visible.map(({ rule, index: ruleIndex }) => {
            const open = openUids.has(rule._uid)
            const Chevron = open ? ChevronDown : ChevronRight
            const label = rule.name || 'Unnamed group'
            const problem = ruleProblem(rule)
            return (
              <li key={rule._uid}>
                <div className="flex items-center gap-2 px-3 py-1.5">
                  <button
                    type="button"
                    aria-expanded={open}
                    aria-label={`${open ? 'Close' : 'Edit'} group rule "${label}"`}
                    onClick={() => toggleOpen(rule._uid)}
                    className="flex min-w-0 flex-1 items-center gap-2 text-left text-body-sm"
                  >
                    <Chevron className="size-3.5 shrink-0" style={{ color: 'var(--fg-subtle)' }} aria-hidden="true" />
                    <span className="w-40 shrink-0 truncate font-medium">{label}</span>
                    <span className="mono min-w-0 flex-1 truncate text-caption text-fg-tertiary">
                      {conditionSummary(rule)}
                    </span>
                    {problem ? (
                      // Rules start closed, so a blank value the save rejects
                      // needs a marker on the line itself.
                      <span className="shrink-0 text-caption text-danger">
                        {problem}
                      </span>
                    ) : (
                      <span className="hidden shrink-0 text-caption sm:inline text-fg-tertiary">
                        {rule.condition_logic === 'all' ? 'all match' : 'any matches'}
                      </span>
                    )}
                  </button>
                  <IconButton
                    type="button"
                    variant="ghost"
                    size="icon-sm"
                    className="text-fg-tertiary hover:text-destructive"
                    label={`Remove group rule "${rule.name}"`}
                    onClick={() => onChange(rules.filter((_, index) => index !== ruleIndex))}
                  >
                    <Trash2 className="size-3.5" aria-hidden />
                  </IconButton>
                </div>
                {open && (
                  <div className="space-y-3 border-t px-3 py-3 border-border-subtle">
                    <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_8rem]">
                      <div className="grid gap-1">
                        <Label htmlFor={`group-name-${rule._uid}`}>Group name</Label>
                        <Input
                          id={`group-name-${rule._uid}`}
                          value={rule.name}
                          onChange={event => updateRule(ruleIndex, { name: event.target.value })}
                          placeholder="e.g. button events"
                        />
                      </div>
                      <div className="grid gap-1">
                        <Label htmlFor={`match-${rule._uid}`}>Match</Label>
                        <NativeSelect
                          id={`match-${rule._uid}`}
                          width="fill"
                          value={rule.condition_logic}
                          onChange={value => updateRule(ruleIndex, {
                            condition_logic: value as EventGroupRule['condition_logic'],
                          })}
                          options={MATCH_OPTIONS}
                        />
                      </div>
                    </div>
                    <div className="space-y-2">
                      {rule.conditions.map((condition, conditionIndex) => (
                        <div key={condition._uid} className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto]">
                          <div className="grid gap-1">
                            <Label htmlFor={`field-${rule._uid}-${condition._uid}`}>Field</Label>
                            <NativeSelect
                              id={`field-${rule._uid}-${condition._uid}`}
                              width="fill"
                              value={condition.field}
                              onChange={value => updateCondition(ruleIndex, conditionIndex, {
                                field: value,
                              })}
                              options={fieldOptions}
                            />
                          </div>
                          <div className="grid gap-1">
                            <Label htmlFor={`regex-${rule._uid}-${condition._uid}`}>Regex</Label>
                            <Input
                              id={`regex-${rule._uid}-${condition._uid}`}
                              value={condition.pattern}
                              onChange={event => updateCondition(ruleIndex, conditionIndex, {
                                pattern: event.target.value,
                              })}
                              placeholder="e.g. ^button:"
                            />
                          </div>
                          <IconButton
                            type="button"
                            variant="ghost"
                            className="self-end text-fg-tertiary hover:text-destructive"
                            label="Remove condition"
                            disabled={rule.conditions.length === 1}
                            onClick={() => updateRule(ruleIndex, {
                              conditions: rule.conditions.filter((_, index) => index !== conditionIndex),
                            })}
                          >
                            <Trash2 className="size-4" aria-hidden />
                          </IconButton>
                        </div>
                      ))}
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={() => updateRule(ruleIndex, {
                          conditions: [...rule.conditions, emptyGroupCondition()],
                        })}
                      >
                        <Plus className="size-3.5" aria-hidden="true" />Add condition
                      </Button>
                    </div>
                  </div>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
