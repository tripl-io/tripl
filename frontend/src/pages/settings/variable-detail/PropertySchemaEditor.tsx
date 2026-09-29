import { useId, useState } from 'react'
import { Plus, Trash2 } from 'lucide-react'
import { NativeSelect } from '@/components/settings/kit'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { IconButton } from '@/components/ui/icon-button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  describeConstraints,
  nodeOfType,
  rootTypeOptions,
  SCHEMA_NODE_TYPE_LABELS,
  SCHEMA_NODE_TYPES,
  STRING_FORMATS,
  type SchemaNodeType,
} from '@/lib/propertySchema'
import { cn } from '@/lib/utils'
import type { PropertySchema, VariableType } from '@/types'

/** Where a node sits decides what it may be: the root follows `variable_type`. */
interface NodeRules {
  typeOptions: SchemaNodeType[]
  /** A date type pins the format; the editor shows it and offers no choice. */
  fixedFormat?: string
  /** A plain string property may not use a date format: that is what the date types are for. */
  noDateFormats?: boolean
  /** The array types pin the item type (number_array: number or integer). */
  itemTypeOptions?: SchemaNodeType[]
}

function rootRules(variableType: VariableType): NodeRules {
  switch (variableType) {
    case 'date': return { typeOptions: ['string'], fixedFormat: 'date' }
    case 'datetime': return { typeOptions: ['string'], fixedFormat: 'date-time' }
    case 'string': return { typeOptions: ['string'], noDateFormats: true }
    case 'string_array': return { typeOptions: ['array'], itemTypeOptions: ['string'] }
    case 'number_array': return { typeOptions: ['array'], itemTypeOptions: ['number', 'integer'] }
    default: return { typeOptions: rootTypeOptions(variableType) }
  }
}

const ANY_NODE: NodeRules = { typeOptions: SCHEMA_NODE_TYPES }

/** A number field that keeps "unset" apart from 0. */
function NumberField({
  label,
  value,
  onChange,
  integer = false,
}: {
  label: string
  value: number | undefined
  onChange: (value: number | undefined) => void
  integer?: boolean
}) {
  const id = useId()
  return (
    <div className="grid gap-1">
      <Label htmlFor={id} className="text-caption text-fg-muted">{label}</Label>
      <Input
        id={id}
        type="number"
        inputMode={integer ? 'numeric' : 'decimal'}
        min={integer ? 0 : undefined}
        step={integer ? 1 : 'any'}
        className="h-8"
        value={value ?? ''}
        onChange={(e) => {
          const raw = e.target.value
          if (raw === '') return onChange(undefined)
          const parsed = Number(raw)
          if (Number.isFinite(parsed)) onChange(integer ? Math.trunc(parsed) : parsed)
        }}
      />
    </div>
  )
}

/** A nested property's name, committed on blur so a half-typed rename never collides. */
function PropertyNameInput({
  name,
  taken,
  onRename,
  label,
}: {
  name: string
  taken: (candidate: string) => boolean
  onRename: (next: string) => void
  label: string
}) {
  const [draft, setDraft] = useState(name)
  const [error, setError] = useState<string | null>(null)
  const [seen, setSeen] = useState(name)
  if (seen !== name) {
    setSeen(name)
    setDraft(name)
  }
  const errorId = useId()
  const commit = () => {
    const next = draft.trim()
    if (next === name) return setError(null)
    if (!next) {
      setError('A nested property needs a name.')
      setDraft(name)
      return
    }
    if (taken(next)) {
      setError(`${next} is already a property here.`)
      setDraft(name)
      return
    }
    setError(null)
    onRename(next)
  }
  return (
    <div className="grid min-w-0 gap-0.5">
      <Input
        aria-label={label}
        className="mono h-8"
        value={draft}
        maxLength={200}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault()
            commit()
          }
        }}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? errorId : undefined}
      />
      {error && <p id={errorId} className="text-caption text-destructive">{error}</p>}
    </div>
  )
}

function SchemaNodeEditor({
  node,
  onChange,
  rules,
  path,
  depth,
}: {
  node: PropertySchema
  onChange: (next: PropertySchema) => void
  rules: NodeRules
  /** Accessible names say which node a control edits: "items of tags", "address.city". */
  path: string
  depth: number
}) {
  const typeId = useId()
  const formatId = useId()
  const patternId = useId()
  const set = (patch: Partial<PropertySchema>) => onChange({ ...node, ...patch })
  const formats = STRING_FORMATS.filter((f) => !rules.noDateFormats || (f !== 'date' && f !== 'date-time'))

  return (
    <div className={cn('grid gap-3', depth > 0 && 'border-l-2 border-border-subtle pl-3')}>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className="grid gap-1">
          <Label htmlFor={typeId} className="text-caption text-fg-muted">Schema type</Label>
          <NativeSelect
            id={typeId}
            width="fill"
            aria-label={`Schema type of ${path}`}
            value={node.type}
            disabled={rules.typeOptions.length < 2}
            onChange={(value) => onChange(nodeOfType(value as SchemaNodeType, node))}
            options={rules.typeOptions.map((t) => ({ value: t, label: SCHEMA_NODE_TYPE_LABELS[t] }))}
          />
        </div>
        {node.type === 'string' && (
          <div className="grid gap-1">
            <Label htmlFor={formatId} className="text-caption text-fg-muted">Format</Label>
            <NativeSelect
              id={formatId}
              width="fill"
              aria-label={`Format of ${path}`}
              value={rules.fixedFormat ?? node.format ?? ''}
              disabled={!!rules.fixedFormat}
              onChange={(value) => set({ format: value || undefined })}
              options={
                rules.fixedFormat
                  ? [{ value: rules.fixedFormat, label: rules.fixedFormat }]
                  : [{ value: '', label: 'Any text' }, ...formats.map((f) => ({ value: f, label: f }))]
              }
            />
          </div>
        )}
      </div>

      {node.type === 'string' && (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)_minmax(0,1fr)]">
          <div className="grid gap-1">
            <Label htmlFor={patternId} className="text-caption text-fg-muted">Pattern (regex)</Label>
            <Input
              id={patternId}
              className="mono h-8"
              placeholder="e.g. ^[A-Z]{3}$"
              maxLength={500}
              value={node.pattern ?? ''}
              onChange={(e) => set({ pattern: e.target.value || undefined })}
            />
          </div>
          <NumberField label="Min length" integer value={node.minLength} onChange={(v) => set({ minLength: v })} />
          <NumberField label="Max length" integer value={node.maxLength} onChange={(v) => set({ maxLength: v })} />
        </div>
      )}

      {(node.type === 'number' || node.type === 'integer') && (
        <div className="grid grid-cols-2 gap-3">
          <NumberField label="Minimum" value={node.minimum} onChange={(v) => set({ minimum: v })} />
          <NumberField label="Maximum" value={node.maximum} onChange={(v) => set({ maximum: v })} />
        </div>
      )}

      {node.type === 'array' && (
        <>
          <div className="grid grid-cols-2 gap-3">
            <NumberField label="Min items" integer value={node.minItems} onChange={(v) => set({ minItems: v })} />
            <NumberField label="Max items" integer value={node.maxItems} onChange={(v) => set({ maxItems: v })} />
          </div>
          <div className="grid gap-1.5">
            <div className="text-caption font-medium text-fg-muted">Items</div>
            <SchemaNodeEditor
              node={node.items ?? { type: rules.itemTypeOptions?.[0] ?? 'string' }}
              onChange={(items) => set({ items })}
              rules={rules.itemTypeOptions ? { typeOptions: rules.itemTypeOptions } : ANY_NODE}
              path={`items of ${path}`}
              depth={depth + 1}
            />
          </div>
        </>
      )}

      {node.type === 'object' && (
        <ObjectPropertiesEditor node={node} onChange={onChange} path={path} depth={depth} />
      )}
    </div>
  )
}

function ObjectPropertiesEditor({
  node,
  onChange,
  path,
  depth,
}: {
  node: PropertySchema
  onChange: (next: PropertySchema) => void
  path: string
  depth: number
}) {
  const properties = node.properties ?? {}
  const names = Object.keys(properties)
  const required = new Set(node.required ?? [])

  const write = (nextProperties: Record<string, PropertySchema>, nextRequired: string[]) =>
    onChange({ ...node, properties: nextProperties, required: nextRequired })

  const add = () => {
    let n = names.length + 1
    while (`property_${n}` in properties) n++
    write({ ...properties, [`property_${n}`]: { type: 'string' } }, [...required])
  }
  const rename = (from: string, to: string) => {
    // Rebuilt in order, so the renamed row stays where it was.
    const next = Object.fromEntries(names.map((name) => [name === from ? to : name, properties[name]!]))
    write(next, [...required].map((name) => (name === from ? to : name)))
  }
  const remove = (name: string) => {
    const next = { ...properties }
    delete next[name]
    write(next, [...required].filter((r) => r !== name))
  }

  return (
    <div className="grid gap-2">
      <div className="text-caption font-medium text-fg-muted">
        Nested properties
        <span className="font-normal text-fg-tertiary"> · one property, its shape described here</span>
      </div>
      {names.length === 0 && (
        <p className="text-caption text-fg-tertiary">No nested properties described: any object is accepted.</p>
      )}
      <ul className="grid gap-3">
        {names.map((name) => {
          const childPath = path === 'the property' ? name : `${path}.${name}`
          return (
            <li key={name} className="grid gap-2 rounded-md border bg-background p-2.5">
              <div className="grid grid-cols-[minmax(0,1fr)_auto_auto] items-start gap-2">
                <PropertyNameInput
                  name={name}
                  label={`Name of nested property ${childPath}`}
                  taken={(candidate) => candidate in properties}
                  onRename={(next) => rename(name, next)}
                />
                <label className="flex h-8 items-center gap-1.5 text-caption text-fg-muted">
                  <Checkbox
                    checked={required.has(name)}
                    aria-label={`${childPath} is required`}
                    onCheckedChange={(checked) =>
                      write(
                        properties,
                        checked === true ? [...required, name] : [...required].filter((r) => r !== name),
                      )
                    }
                  />
                  Required
                </label>
                <IconButton
                  type="button"
                  variant="ghost"
                  className="size-8 text-fg-tertiary hover:text-destructive"
                  label={`Remove nested property ${childPath}`}
                  onClick={() => remove(name)}
                >
                  <Trash2 className="size-3.5" aria-hidden="true" />
                </IconButton>
              </div>
              <SchemaNodeEditor
                node={properties[name]!}
                onChange={(sub) => write({ ...properties, [name]: sub }, [...required])}
                rules={ANY_NODE}
                path={childPath}
                depth={depth + 1}
              />
            </li>
          )
        })}
      </ul>
      <div>
        <Button type="button" variant="outline" size="sm" onClick={add}>
          <Plus className="size-3.5" aria-hidden="true" />
          Add nested property
        </Button>
      </div>
    </div>
  )
}

/**
 * The property's type as JSON Schema: the root follows the property's type
 * (a number may narrow to integer, JSON is an object or an array, the date
 * types pin their format), nested objects describe their own properties.
 * `error` is the server's refusal of the last save, shown where it applies.
 */
export function PropertySchemaEditor({
  variableType,
  schema,
  onChange,
  problems,
  error,
}: {
  variableType: VariableType
  schema: PropertySchema
  onChange: (next: PropertySchema) => void
  /** What the editor already sees wrong (bounds, regex, names). */
  problems: string[]
  error?: string | null
}) {
  return (
    <fieldset className="grid gap-2 rounded-md border bg-muted/30 p-3">
      <legend className="px-1 text-body-sm font-medium">Schema</legend>
      <p className="text-caption text-fg-tertiary">
        What a value of this property looks like. Scans, exports and codegen read it; documented
        values stay in the list below.
      </p>
      <SchemaNodeEditor
        node={schema}
        onChange={onChange}
        rules={rootRules(variableType)}
        path="the property"
        depth={0}
      />
      {problems.length > 0 && (
        <ul role="alert" className="grid gap-0.5 text-body-sm text-destructive">
          {problems.map((problem) => <li key={problem}>{problem}</li>)}
        </ul>
      )}
      {error && <p role="alert" className="text-body-sm text-destructive">{error}</p>}
    </fieldset>
  )
}

/** The schema in words, for a viewer. */
export function PropertySchemaSummary({ schema }: { schema: PropertySchema }) {
  const constraints = describeConstraints(schema)
  return (
    <div className="grid gap-1">
      <span>
        {SCHEMA_NODE_TYPE_LABELS[schema.type]}
        {schema.format ? ` (${schema.format})` : ''}
        {schema.type === 'array' && schema.items ? ` of ${SCHEMA_NODE_TYPE_LABELS[schema.items.type]}` : ''}
        {constraints.length > 0 ? ` · ${constraints.join(', ')}` : ''}
      </span>
      {schema.type === 'object' && schema.properties && Object.keys(schema.properties).length > 0 && (
        <ul className="grid gap-0.5 border-l-2 border-border-subtle pl-3">
          {Object.entries(schema.properties).map(([name, sub]) => (
            <li key={name}>
              <span className="mono">{name}</span>
              {schema.required?.includes(name) ? <span className="text-fg-tertiary"> (required)</span> : null}
              {': '}
              <PropertySchemaSummary schema={sub} />
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
