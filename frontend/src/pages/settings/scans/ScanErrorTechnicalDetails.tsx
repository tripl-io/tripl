import { useIsOwner } from '@/lib/permissions'

/**
 * The raw scan error behind `friendlyScanError`'s safe message, for an owner.
 *
 * The sanitiser keeps `technical` for exactly this expander, but it only
 * existed on the projects dashboard: on the scan's own pages an unmapped error
 * collapsed to "Scan failed." and the owner had nothing to debug it with.
 * Owner-only because the raw text can name hosts, ports and drivers.
 */
export function ScanErrorTechnicalDetails({ technical }: { technical: string | undefined }) {
  const isOwner = useIsOwner()
  if (!isOwner || !technical) return null
  return (
    <details className="mt-1.5 text-caption text-fg-secondary">
      <summary className="cursor-pointer select-none">View technical details</summary>
      <p className="mono mt-1 whitespace-pre-wrap break-words">{technical}</p>
    </details>
  )
}
