import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'

const example = {
  table: 'analytics.events', event_column: 'name', platform_column: 'platform',
  build_column: 'build', time_column: 'ts', dedup_column: 'order_id',
  rules: [
    { event: 'checkout_success', platform: 'ios', min_build: 1, max_build: 120 },
    { event: 'order_completed', platform: 'ios', min_build: 110, max_build: 999 },
  ],
}

export function mappingError(value: string) {
  if (!value) return null
  if (new TextEncoder().encode(value).length > 2000) return 'Mapping exceeds 2000 bytes.'
  try {
    const parsed: unknown = JSON.parse(value)
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) return null
  } catch { /* Share the same validation message for malformed and scalar JSON. */ }
  return 'Enter a JSON object.'
}

function arg(value: string) {
  return /^[a-zA-Z0-9_-]+$/.test(value) ? value : "'" + value.replaceAll("'", "'\\''") + "'"
}

export function CompatibilityMapping({ id, value, onChange, eventId, slug, branchId }: {
  id: string; value: string; onChange: (value: string) => void; eventId?: string;
  slug?: string; branchId?: string | null
}) {
  const error = mappingError(value)
  const target = eventId && slug ? `${arg(eventId)} --project=${arg(slug)}` : null
  return <div className="grid gap-2">
    <Textarea id={id} value={value} aria-invalid={!!error}
      onChange={e => onChange(e.target.value)} />
    {error && <p role="alert">{error}</p>}
    {!value && <Button type="button" variant="outline" onClick={() => onChange(JSON.stringify(example, null, 2))}>Insert example</Button>}
    {target && <code>tripl compatibility preview {target}{branchId && ` --branch=${arg(branchId)}`} --start YYYY-MM-DD --end YYYY-MM-DD</code>}
    {target && !branchId && <code>tripl compatibility model {target}</code>}
  </div>
}
