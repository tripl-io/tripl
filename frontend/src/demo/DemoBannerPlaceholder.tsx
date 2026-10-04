/**
 * The demo banner's box, held while its lazy chunk loads (#251).
 *
 * The banner is its own chunk, so on a hard load the page rendered first and
 * the bar arrived ~1s later, pushing everything down 62px just as the user
 * started reading. The shell's Suspense boundary shows this instead: the same
 * footprint — the phone pill below `lg`, the one-line bar from `lg` up, and
 * the banner's bottom margin — so nothing moves when the real one lands.
 *
 * The bar is the banner's own structure — a bordered panel around an `h-11`
 * row — so its 2px of border are counted the same way: a bare `h-11` box was
 * 44px against the banner's 46.
 *
 * It carries `data-demo-banner` like the real one, so a docked coach card that
 * mounts before the banner's chunk already starts below it (#251).
 *
 * Deliberately tiny and dependency-free: the shell imports it statically,
 * into the entry chunk.
 */
export function DemoBannerPlaceholder() {
  const tone = { background: 'var(--warning-soft)', borderColor: 'var(--warning)' }
  return (
    <div
      className="mb-4"
      aria-hidden="true"
      data-testid="demo-banner-placeholder"
      data-demo-banner=""
    >
      <div className="h-8 w-24 rounded-full border lg:hidden" style={tone} />
      <div className="hidden rounded-lg border lg:block" style={tone}>
        <div className="h-11" />
      </div>
    </div>
  )
}
