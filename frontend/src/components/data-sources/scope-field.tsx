import type { ReactNode } from 'react'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { FieldError } from '@/components/forms/FieldError'
import { invalidAria } from '@/components/forms/validation'
import { FIELD_COL_CLASS, HELP_CLASS } from './connection-settings'

interface ScopeFieldProps {
  id: string
  /** "Default schema", or the allowlist's name: schemas, Glue databases, datasets. */
  label: string
  value: string
  onChange: (value: string) => void
  placeholder: string
  help: ReactNode
  /** Why the value cannot be saved, shown under it. */
  error?: string
}

/**
 * One field of what a connection reads: the default schema unqualified names
 * resolve in, or an allowlist of what the schema browser may list. Every
 * warehouse that has one renders it here, with its own words in `help`.
 */
export function ScopeField({ id, label, value, onChange, placeholder, help, error }: ScopeFieldProps) {
  return (
    <div className={FIELD_COL_CLASS}>
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        {...invalidAria(id, error)}
      />
      <FieldError inputId={id} message={error} />
      <p className={HELP_CLASS}>{help}</p>
    </div>
  )
}
