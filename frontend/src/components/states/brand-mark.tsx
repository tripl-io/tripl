/**
 * tripl service mark — a single triangle split into three teal facets ("tri").
 * Built on the active `--accent` so it re-tints with the chosen accent theme:
 * a lightened facet, the accent itself, and a darkened facet. Shared by the
 * sidebar and the shell-less states (project not found).
 */
export function TrifoldMark({ size = 24 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 100 100"
      aria-hidden="true"
      className="block shrink-0"
    >
      <polygon points="50,15 14,85 50,61.7" fill="color-mix(in oklab, var(--accent) 60%, white)" />
      <polygon points="50,15 86,85 50,61.7" fill="color-mix(in oklab, var(--accent) 80%, black)" />
      <polygon points="14,85 86,85 50,61.7" fill="var(--accent)" />
    </svg>
  )
}
