/**
 * The demo guide's face: the product's three-facet mark, given eyes and a
 * smile. Decorative — the guide's text carries everything it says — so it is
 * hidden from assistive technology. It blinks and breathes (`.guide-eyes`,
 * `.guide-body` in index.css), and holds still under reduced motion.
 */
export function GuideMascot({ size = 40 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 100 100"
      aria-hidden="true"
      className="block shrink-0 overflow-visible"
    >
      <ellipse cx="50" cy="94" rx="26" ry="4" fill="var(--fg)" opacity="0.14" />
      <g className="guide-body">
        <polygon
          points="50,8 10,84 50,60"
          fill="color-mix(in oklab, var(--accent) 62%, white)"
          stroke="color-mix(in oklab, var(--accent) 62%, white)"
          strokeWidth="6"
          strokeLinejoin="round"
        />
        <polygon
          points="50,8 90,84 50,60"
          fill="color-mix(in oklab, var(--accent) 78%, black)"
          stroke="color-mix(in oklab, var(--accent) 78%, black)"
          strokeWidth="6"
          strokeLinejoin="round"
        />
        <polygon
          points="10,84 90,84 50,60"
          fill="var(--accent)"
          stroke="var(--accent)"
          strokeWidth="6"
          strokeLinejoin="round"
        />
        <g className="guide-eyes">
          <ellipse cx="40" cy="50" rx="6.5" ry="7.5" fill="white" />
          <ellipse cx="60" cy="50" rx="6.5" ry="7.5" fill="white" />
          <circle cx="41.5" cy="51.5" r="3.4" fill="oklch(0.2 0.02 250)" />
          <circle cx="61.5" cy="51.5" r="3.4" fill="oklch(0.2 0.02 250)" />
          <circle cx="42.8" cy="49.8" r="1.1" fill="white" />
          <circle cx="62.8" cy="49.8" r="1.1" fill="white" />
        </g>
        <path
          d="M 43 70 Q 50 76 57 70"
          fill="none"
          stroke="oklch(0.2 0.02 250)"
          strokeWidth="3"
          strokeLinecap="round"
        />
      </g>
    </svg>
  )
}
