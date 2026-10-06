/// <reference types="node" />
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * Guards the WCAG AA floor for the text tokens in `styles/tokens.css`,
 * extended to the status tones.
 *
 * `--fg-subtle` and `--fg-faint` are not decoration: they carry activity
 * timestamps, chart captions, search placeholders, sidebar section labels and
 * the "—" empty-cell markers, all at 10–12.5px. WCAG 2.x SC 1.4.3 asks for
 * 4.5:1 at those sizes, so the tokens are pinned by measurement rather than by
 * eye. This test reads the real stylesheet, so drifting a token back down
 * fails here instead of on a phone.
 *
 * The status tones are held to the same bar in the hardest place they appear:
 * `--<tone>` as text on its own `--<tone>-soft` fill. That pairing IS the
 * status-chip vocabulary (Chip, Dot, Badge, and every `bg-*-soft text-*` call
 * site), and tinting a surface with the same hue as the text is what eats the
 * ratio, so it is the case worth pinning.
 */

// Only the body floor is left: `--accent` used to be held to AA-large on the
// bare background, on the theory that it is decoration. It is not — it carries
// icons, links and chip labels at body size — so it now answers to AA_BODY like
// everything else here.
const AA_BODY = 4.5

// WCAG 2.x SC 1.4.11: the boundary of a user-interface component needs 3:1. It
// is a lower bar than body text and it applies to things carrying no text at
// all — which is exactly `--input`, the only mark on an unfocused, transparent
// form field and the whole of an unchecked Switch track.
const AA_NON_TEXT = 3

// Surfaces that body copy is actually painted on. `--surface-active` is a
// momentary pressed state that never hosts small text, so it is excluded.
const TEXT_SURFACES = [
  '--bg',
  '--bg-sunken',
  '--bg-elevated',
  '--surface',
  '--surface-hover',
] as const

// The three text steps. The four old names are `var()` aliases of
// these and are checked as such below, so measuring the steps covers them.
const BODY_TEXT_TOKENS = ['--fg', '--fg-secondary', '--fg-tertiary'] as const

// Every tone that carries status *text*. `--<tone>` is the ink, `--<tone>-soft`
// the fill it sits on. `--accent` is held to the same bar further down, where
// it also has to answer for the solid fill it paints.
const STATUS_TONES = ['success', 'warning', 'danger', 'info'] as const

// The brand hue is switchable, so every variant is a separate palette that has
// to clear the floor on its own — `:root` / `.dark` carry the default (teal),
// and each `.accent-*` class overrides it. Miss one and a user who picked lime
// gets a theme nobody measured.
const ACCENT_VARIANTS = ['teal', 'violet', 'lime', 'indigo', 'magenta'] as const
const ACCENT_BLOCKS = [
  { name: 'default', light: ':root', dark: '.dark' },
  ...ACCENT_VARIANTS.map((variant) => ({
    name: variant,
    light: `.accent-${variant}`,
    dark: `.dark.accent-${variant}`,
  })),
] as const

type Rgb = readonly [number, number, number]

// Read the shipped stylesheet directly: `?raw` goes through the Tailwind
// plugin, which rewrites the file, so the token declarations would be gone.
const css = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), 'styles/tokens.css'),
  'utf8',
)

/**
 * Grab the first `<selector> { … }` block body from the stylesheet. Ends at the
 * first `}`, so the one-line `.accent-*` variants read the same way the
 * multi-line theme blocks do; none of these blocks nest.
 */
function block(selector: string): string {
  const start = css.indexOf(`${selector} {`)
  if (start < 0) throw new Error(`no ${selector} block in styles/tokens.css`)
  const end = css.indexOf('}', start)
  if (end < 0) throw new Error(`unterminated ${selector} block in styles/tokens.css`)
  return css.slice(start, end)
}

type Oklch = { lightness: number; chroma: number; hueDeg: number; alpha: number }

/** Read `--token: oklch(L C H)` — or `oklch(L C H / A)` — out of a block body. */
function oklchDecl(body: string, token: string): Oklch {
  const match = new RegExp(
    `${token}:\\s*oklch\\(([\\d.]+)\\s+([\\d.]+)\\s+([\\d.]+)(?:\\s*/\\s*([\\d.]+))?\\)`,
  ).exec(body)
  if (!match) throw new Error(`token ${token} is not a plain oklch() declaration`)
  return {
    lightness: Number(match[1]),
    chroma: Number(match[2]),
    hueDeg: Number(match[3]),
    alpha: match[4] === undefined ? 1 : Number(match[4]),
  }
}

/** The opaque sRGB color of a token, ignoring any alpha it carries. */
function oklchToken(body: string, token: string): Rgb {
  const { lightness, chroma, hueDeg } = oklchDecl(body, token)
  return oklchToSrgb(lightness, chroma, hueDeg)
}

/** oklch → oklab → linear sRGB (Björn Ottosson's matrices), before any clamp. */
function oklchToLinearRgb(lightness: number, chroma: number, hueDeg: number): number[] {
  const hue = (hueDeg * Math.PI) / 180
  const a = chroma * Math.cos(hue)
  const b = chroma * Math.sin(hue)
  const l = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
  const m = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
  const s = (lightness - 0.0894841775 * a - 1.291485548 * b) ** 3
  return [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
  ]
}

/** …then gamma-encoded and clamped into 8-bit sRGB. */
function oklchToSrgb(lightness: number, chroma: number, hueDeg: number): Rgb {
  const encode = (v: number) =>
    v <= 0.0031308 ? 12.92 * v : 1.055 * Math.max(v, 0) ** (1 / 2.4) - 0.055
  const channels = oklchToLinearRgb(lightness, chroma, hueDeg).map((v) =>
    Math.round(Math.min(1, Math.max(0, encode(v))) * 255),
  )
  return [channels[0]!, channels[1]!, channels[2]!] as const
}

/**
 * Whether a declaration lands inside sRGB. Outside it the browser gamut-maps
 * (chroma reduction against ΔE-OK), which this file's clamp does not model — so
 * a ratio measured here for an out-of-gamut color is not the painted one.
 */
function isInSrgbGamut({ lightness, chroma, hueDeg }: Oklch): boolean {
  return oklchToLinearRgb(lightness, chroma, hueDeg).every((v) => v >= -0.0005 && v <= 1.0005)
}

/**
 * Source-over compositing of a translucent fill onto an opaque surface.
 *
 * In gamma-encoded sRGB, not linear-light: painting a translucent background
 * over an opaque backdrop happens in the backdrop's color space (linear-light
 * is for `mix-blend-mode` and filters). Verified against painted pixels rather
 * than read off the spec — screenshotting these exact pairs in Chromium and
 * predicting them both ways gives a total absolute channel error, over 20
 * samples per theme, of 28 (light) / 13 (dark) for the model below against
 * 608 / 1666 for linear-light. The first is rounding; the second is a
 * different color.
 */
function composite(fill: Oklch, surface: Rgb): Rgb {
  const rgb = oklchToSrgb(fill.lightness, fill.chroma, fill.hueDeg)
  const mix = (i: number) => Math.round(fill.alpha * rgb[i]! + (1 - fill.alpha) * surface[i]!)
  return [mix(0), mix(1), mix(2)] as const
}

function relativeLuminance([r, g, b]: Rgb): number {
  const linear = (channel: number) => {
    const c = channel / 255
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b)
}

export function contrastRatio(fg: Rgb, bg: Rgb): number {
  const a = relativeLuminance(fg)
  const b = relativeLuminance(bg)
  const [hi, lo] = a > b ? [a, b] : [b, a]
  return (hi + 0.05) / (lo + 0.05)
}

const THEMES = [
  { name: 'light', body: block(':root') },
  { name: 'dark', body: block('.dark') },
] as const

describe.each(THEMES)('$name theme text tokens', ({ body }) => {
  it.each(BODY_TEXT_TOKENS)('%s clears WCAG AA on every text surface', (token) => {
    const fg = oklchToken(body, token)
    for (const surface of TEXT_SURFACES) {
      const ratio = contrastRatio(fg, oklchToken(body, surface))
      expect(
        ratio,
        `${token} on ${surface} measured ${ratio.toFixed(2)}:1`,
      ).toBeGreaterThanOrEqual(AA_BODY)
    }
  })

  it('keeps --input at the non-text floor, so a field has a findable edge', () => {
    // --input used to alias --border, the row-separator hairline, which measures
    // ~1.2-1.3:1: every text field, textarea and select was drawn with an edge
    // a low-vision reader could not locate. Nothing in this file measured a
    // border token, so nothing would have caught it.
    const input = oklchToken(body, '--input')
    for (const surface of TEXT_SURFACES) {
      const ratio = contrastRatio(input, oklchToken(body, surface))
      expect(
        ratio,
        `--input on ${surface} measured ${ratio.toFixed(2)}:1`,
      ).toBeGreaterThanOrEqual(AA_NON_TEXT)
    }
  })

  // `--surface-active` is the selected / pressed row. It is not in
  // TEXT_SURFACES because the tones and accents are never measured there, but
  // captions and timestamps do sit on a selected row (DS-A).
  it.each(BODY_TEXT_TOKENS)('%s clears WCAG AA on a selected row', (token) => {
    const ratio = contrastRatio(oklchToken(body, token), oklchToken(body, '--surface-active'))
    expect(ratio, `${token} on --surface-active measured ${ratio.toFixed(2)}:1`)
      .toBeGreaterThanOrEqual(AA_BODY)
  })

  it('keeps the prominence order --fg > --fg-secondary > --fg-tertiary', () => {
    const surface = oklchToken(body, '--surface')
    const ratios = BODY_TEXT_TOKENS.map((token) => contrastRatio(oklchToken(body, token), surface))
    for (let i = 1; i < ratios.length; i += 1) {
      expect(ratios[i]!, `${BODY_TEXT_TOKENS[i]} must be dimmer than ${BODY_TEXT_TOKENS[i - 1]}`)
        .toBeLessThan(ratios[i - 1]!)
    }
  })
})

describe.each(THEMES)('$name theme status tones', ({ body }) => {
  it.each(STATUS_TONES)(
    '--%s clears WCAG AA as text on its own soft fill',
    (tone) => {
      const ink = oklchToken(body, `--${tone}`)
      const fill = oklchDecl(body, `--${tone}-soft`)
      for (const surface of TEXT_SURFACES) {
        const ratio = contrastRatio(ink, composite(fill, oklchToken(body, surface)))
        expect(
          ratio,
          `--${tone} on --${tone}-soft over ${surface} measured ${ratio.toFixed(2)}:1`,
        ).toBeGreaterThanOrEqual(AA_BODY)
      }
    },
  )

  // The same ink without the fill: `text-warning` on a plain row is as common
  // as the chip, and the fill is not what rescues it.
  it.each(STATUS_TONES)('--%s clears WCAG AA on an untinted surface', (tone) => {
    const ink = oklchToken(body, `--${tone}`)
    for (const surface of TEXT_SURFACES) {
      const ratio = contrastRatio(ink, oklchToken(body, surface))
      expect(
        ratio,
        `--${tone} on ${surface} measured ${ratio.toFixed(2)}:1`,
      ).toBeGreaterThanOrEqual(AA_BODY)
    }
  })

  // Guards the measurement itself rather than the design: see isInSrgbGamut.
  it.each(STATUS_TONES)('--%s and its soft fill stay inside sRGB', (tone) => {
    for (const token of [`--${tone}`, `--${tone}-soft`]) {
      expect(isInSrgbGamut(oklchDecl(body, token)), `${token} is outside sRGB`).toBe(true)
    }
  })
})

/**
 * The destructive button is the one place a tone is painted as a SOLID fill
 * with a label on top, so it needs a foreground that flips with the theme —
 * `--danger` itself cannot move, being pinned above as status ink on its own
 * soft fill. Hover is measured too: `hover:bg-destructive/90` lets the surface
 * through, which moves the fill toward the background.
 */
describe('destructive button', () => {
  it('aliases --destructive to --danger, which is what the cases below measure', () => {
    expect(block(':root')).toMatch(/--destructive:\s*var\(--danger\)/)
  })

  it.each(THEMES)('$name label clears WCAG AA on the fill and on hover', ({ name, body }) => {
    // The foreground is declared per theme and inherited from :root otherwise.
    const declaredIn = /--destructive-foreground:/.test(body) ? body : block(':root')
    const ink = oklchToken(declaredIn, '--destructive-foreground')
    const fill = oklchDecl(body, '--danger')

    const onFill = contrastRatio(ink, oklchToSrgb(fill.lightness, fill.chroma, fill.hueDeg))
    expect(onFill, `${name} label on --destructive measured ${onFill.toFixed(2)}:1`)
      .toBeGreaterThanOrEqual(AA_BODY)

    for (const surface of TEXT_SURFACES) {
      const hovered = composite({ ...fill, alpha: 0.9 }, oklchToken(body, surface))
      const ratio = contrastRatio(ink, hovered)
      expect(
        ratio,
        `${name} label on hover:bg-destructive/90 over ${surface} measured ${ratio.toFixed(2)}:1`,
      ).toBeGreaterThanOrEqual(AA_BODY)
    }
  })
})

describe('identity chip', () => {
  it('carries white initials at AA against --avatar-bg', () => {
    const token = oklchDecl(block(':root'), '--avatar-bg')
    expect(isInSrgbGamut(token), '--avatar-bg is outside sRGB').toBe(true)
    const ratio = contrastRatio([255, 255, 255], oklchToken(block(':root'), '--avatar-bg'))
    expect(ratio, `white on --avatar-bg measured ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(
      AA_BODY,
    )
  })
})

/**
 * The brand hue works both ways at once, which is what made it the hard case:
 * it is the ink for `--accent-soft` and for accent-coloured text
 * on a plain row (~48 call sites), *and* the solid fill under `--accent-fg` on
 * every primary button, switch, checkbox and tooltip (~26). Both roles are
 * measured here, for all five switchable variants, because a user who picks
 * `lime` gets a palette nobody else looks at.
 *
 * `--accent-hover` is only ever a fill, so it answers for its label alone.
 */
describe.each(ACCENT_BLOCKS)('accent: $name', ({ light, dark }) => {
  const cases = [
    { theme: 'light', selector: light, surfaces: ':root' },
    { theme: 'dark', selector: dark, surfaces: '.dark' },
  ] as const

  it.each(cases)('$theme reads as ink on its own soft fill', ({ selector, surfaces }) => {
    const body = block(selector)
    const ink = oklchToken(body, '--accent')
    const fill = oklchDecl(body, '--accent-soft')
    for (const surface of TEXT_SURFACES) {
      const base = oklchToken(block(surfaces), surface)
      const onFill = contrastRatio(ink, composite(fill, base))
      expect(
        onFill,
        `${selector} --accent on --accent-soft over ${surface} measured ${onFill.toFixed(2)}:1`,
      ).toBeGreaterThanOrEqual(AA_BODY)

      const onBare = contrastRatio(ink, base)
      expect(
        onBare,
        `${selector} --accent on ${surface} measured ${onBare.toFixed(2)}:1`,
      ).toBeGreaterThanOrEqual(AA_BODY)
    }
  })

  it.each(cases)('$theme carries --accent-fg on the fill and on hover', ({ selector }) => {
    const body = block(selector)
    const label = oklchToken(body, '--accent-fg')
    for (const fill of ['--accent', '--accent-hover'] as const) {
      const ratio = contrastRatio(label, oklchToken(body, fill))
      expect(
        ratio,
        `${selector} --accent-fg on ${fill} measured ${ratio.toFixed(2)}:1`,
      ).toBeGreaterThanOrEqual(AA_BODY)
    }
  })

  it.each(cases)('$theme stays inside sRGB', ({ selector }) => {
    const body = block(selector)
    for (const token of ['--accent', '--accent-hover', '--accent-soft', '--accent-fg'] as const) {
      expect(isInSrgbGamut(oklchDecl(body, token)), `${selector} ${token} is outside sRGB`).toBe(
        true,
      )
    }
  })
})

/**
 * Secondary text on a tinted panel: the demo welcome panel and the
 * provisioning dialog set captions on `--accent-soft` and `--warning-soft`,
 * and the tint is what eats the ratio. Both secondary steps must clear AA
 * there. `--fg-faint` used to fail on a tint and was banned from them; it is
 * `--fg-tertiary` now, which is held to the tint too, so the ban is
 * gone and this keeps it gone.
 */
describe('secondary text on tinted fills', () => {
  const TINTS = ['--accent-soft', '--warning-soft'] as const
  const measure = (body: string, token: string, tint: string) =>
    contrastRatio(oklchToken(body, token), composite(oklchDecl(body, tint), oklchToken(body, '--bg')))

  it.each(THEMES)('$name: --fg-secondary and --fg-tertiary clear AA on each tint', ({ name, body }) => {
    for (const tint of TINTS) {
      for (const token of ['--fg-secondary', '--fg-tertiary'] as const) {
        const ratio = measure(body, token, tint)
        expect(ratio, `${name} ${token} on ${tint} over --bg measured ${ratio.toFixed(2)}:1`)
          .toBeGreaterThanOrEqual(AA_BODY)
      }
    }
  })
})

/**
 * The old four-step names are aliases of the three steps. They are
 * declared once, in :root, and resolve on <html> where the theme class sits,
 * so the dark block must not redeclare them with a literal of its own: that
 * would reopen a fourth, unmeasured grey.
 */
describe('text token aliases', () => {
  it.each([
    ['--fg-muted', '--fg-secondary'],
    ['--fg-subtle', '--fg-tertiary'],
    ['--fg-faint', '--fg-tertiary'],
  ] as const)('%s aliases %s and is not redeclared in .dark', (alias, step) => {
    expect(block(':root')).toMatch(new RegExp(`${alias}:\\s*var\\(${step}\\)`))
    expect(block('.dark')).not.toMatch(new RegExp(`${alias}:`))
  })
})

/**
 * Dark elevation: a floating layer is lighter than the card under it,
 * and a card lighter than the page.
 */
describe('dark elevation ladder', () => {
  it('orders --bg < --surface < --bg-elevated < --surface-hover < --surface-active', () => {
    const body = block('.dark')
    const ladder = ['--bg', '--surface', '--bg-elevated', '--surface-hover', '--surface-active']
    const lightness = ladder.map((token) => oklchDecl(body, token).lightness)
    for (let i = 1; i < ladder.length; i += 1) {
      expect(lightness[i]!, `${ladder[i]} must be lighter than ${ladder[i - 1]}`)
        .toBeGreaterThan(lightness[i - 1]!)
    }
  })
})

/**
 * No accent may pass for a status. With amber the brand colour WAS
 * --warning, and rose sat 7° from --danger, so every primary button, focus
 * ring and selected pill read as a warning or a destructive action. Every
 * accent hue stays 35° or more from every status hue, in both themes.
 */
describe('accent hues stay out of the status bands', () => {
  const MIN_HUE_GAP = 35
  const hueGap = (a: number, b: number) => Math.min(Math.abs(a - b), 360 - Math.abs(a - b))

  it.each(ACCENT_BLOCKS)('$name', ({ light, dark }) => {
    for (const [selector, surfaces] of [[light, ':root'], [dark, '.dark']] as const) {
      const accentHue = oklchDecl(block(selector), '--accent').hueDeg
      for (const tone of STATUS_TONES) {
        const toneHue = oklchDecl(block(surfaces), `--${tone}`).hueDeg
        const gap = hueGap(accentHue, toneHue)
        expect(gap, `${selector} --accent sits ${gap}° from --${tone}`).toBeGreaterThanOrEqual(
          MIN_HUE_GAP,
        )
      }
    }
  })
})

/**
 * The solid accent fill. Light aliases it to --accent, which the
 * accent cases above already measure under --accent-fg; dark gives every
 * variant a deeper fill under white, measured here.
 */
describe('solid accent fill', () => {
  it('aliases the fill to the accent in light', () => {
    expect(block(':root')).toMatch(/--accent-solid:\s*var\(--accent\)/)
    expect(block(':root')).toMatch(/--accent-solid-fg:\s*var\(--accent-fg\)/)
  })

  it.each(ACCENT_BLOCKS)('$name: dark label clears WCAG AA on the fill', ({ dark }) => {
    const body = block(dark)
    const fill = oklchDecl(body, '--accent-solid')
    expect(isInSrgbGamut(fill), `${dark} --accent-solid is outside sRGB`).toBe(true)
    const label = oklchToken(block('.dark'), '--accent-solid-fg')
    const ratio = contrastRatio(label, oklchToken(body, '--accent-solid'))
    expect(ratio, `${dark} --accent-solid-fg on --accent-solid measured ${ratio.toFixed(2)}:1`)
      .toBeGreaterThanOrEqual(AA_BODY)
  })
})

/**
 * Switch tracks (ui/switch, kit Toggle): checked is --accent, unchecked is
 * --input. In dark, --accent-solid sits at --input's lightness (~1.04:1), so a
 * checked track filled with it looked unchecked. The bright accent keeps the
 * two tracks apart. 3:1 between the tracks is out of reach while --input is
 * pinned to 3:1 against the surfaces, so the floor here is 2:1; the thumb's
 * position carries the state too.
 */
describe('switch tracks', () => {
  const TRACK_FLOOR = 2
  it.each(ACCENT_BLOCKS)('$name: dark checked track stands apart from the unchecked one', ({ dark }) => {
    const checked = oklchToken(block(dark), '--accent')
    const unchecked = oklchToken(block('.dark'), '--input')
    const ratio = contrastRatio(checked, unchecked)
    expect(ratio, `${dark} --accent against --input measured ${ratio.toFixed(2)}:1`)
      .toBeGreaterThanOrEqual(TRACK_FLOOR)
  })
})

/**
 * The chart series palette. Lines and dots are non-text
 * graphics, so each slot answers to 3:1 against the surfaces a chart sits on,
 * in both themes. Slots must also stay out of the danger hue — anomaly dots
 * are `--danger` — and be told apart from one another.
 */
describe('chart series tokens', () => {
  const SERIES = Array.from({ length: 8 }, (_, i) => `--series-${i + 1}`)
  const CHART_SURFACES = ['--bg', '--surface', '--bg-elevated'] as const

  it.each(THEMES)('$name: every series clears 3:1 on every chart surface', ({ name, body }) => {
    for (const token of SERIES) {
      for (const surface of CHART_SURFACES) {
        const ratio = contrastRatio(oklchToken(body, token), oklchToken(body, surface))
        expect(ratio, `${name} ${token} on ${surface} measured ${ratio.toFixed(2)}:1`)
          .toBeGreaterThanOrEqual(AA_NON_TEXT)
      }
    }
  })

  it.each(THEMES)('$name: every series is inside sRGB and away from the danger hue', ({ name, body }) => {
    const dangerHue = oklchDecl(body, '--danger').hueDeg
    for (const token of SERIES) {
      const decl = oklchDecl(body, token)
      expect(isInSrgbGamut(decl), `${name} ${token} is outside sRGB`).toBe(true)
      const gap = Math.min(Math.abs(decl.hueDeg - dangerHue), 360 - Math.abs(decl.hueDeg - dangerHue))
      expect(gap, `${name} ${token} hue sits ${gap.toFixed(0)}° from --danger`).toBeGreaterThanOrEqual(30)
    }
  })

  it.each(THEMES)('$name: no two series share a hue', ({ name, body }) => {
    const hues = SERIES.map((token) => oklchDecl(body, token).hueDeg)
    expect(new Set(hues).size, `${name} series hues ${hues.join(', ')}`).toBe(SERIES.length)
  })
})
