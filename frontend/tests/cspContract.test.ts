import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const EXPECTED_CSP =
  "default-src 'self'; script-src 'self'; " +
  "style-src 'self' 'unsafe-inline'; " +
  "img-src 'self' data: blob:; font-src 'self' data:; " +
  "connect-src 'self'; frame-src https://www.figma.com https://embed.figma.com; " +
  "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"

function readFrontendFile(path: string): string {
  return readFileSync(resolve(process.cwd(), path), 'utf8')
}

describe('production CSP contract', () => {
  it('loads executable scripts from the same origin', () => {
    const indexHtml = readFrontendFile('index.html')
    const scriptTags = [...indexHtml.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)]

    expect(scriptTags.length).toBeGreaterThan(0)
    expect(scriptTags.every(([, attributes]) => /\bsrc=/.test(attributes))).toBe(true)
    expect(indexHtml).toContain('<script src="/theme-init.js"></script>')
    expect(indexHtml.indexOf('/theme-init.js')).toBeLessThan(indexHtml.indexOf('/src/main.tsx'))
    expect(readFrontendFile('public/theme-init.js')).not.toHaveLength(0)
  })

  it('points the page at no font host the CSP does not name', () => {
    const indexHtml = readFrontendFile('index.html')
    expect(indexHtml).not.toMatch(/fonts\.(googleapis|gstatic)\.com/)
  })

  // The fonts ship with the bundle: the entry imports every
  // weight index.html used to fetch from Google Fonts, so the CSP can name no
  // third-party font or style host.
  it('self-hosts the UI fonts', () => {
    const main = readFrontendFile('src/main.tsx')
    for (const face of [
      '@fontsource/inter/400.css',
      '@fontsource/inter/500.css',
      '@fontsource/inter/600.css',
      '@fontsource/inter/700.css',
      '@fontsource/jetbrains-mono/400.css',
      '@fontsource/jetbrains-mono/500.css',
    ]) {
      expect(main).toContain(`import '${face}'`)
    }
    // Fontsource declares every face with font-display: swap, as the Google
    // stylesheet's `display=swap` did.
    const face = readFrontendFile('node_modules/@fontsource/inter/400.css')
    expect(face).toContain('font-display: swap')
    expect(EXPECTED_CSP).not.toMatch(/googleapis|gstatic/)
  })

  // The API serves the built SPA itself (app.frontend() in the root
  // Dockerfile's runtime image); the standalone nginx tier is gone, so the
  // backend default is the one production CSP. It drifted from the frontend's
  // needs once — no frame-src, so the Figma embed was blocked (#194).
  it('matches the backend default CSP exactly', () => {
    const backend = readFrontendFile('../backend/src/tripl/middleware/security_headers.py')
    const block = backend.match(/_DEFAULT_SPA_CSP = \(([\s\S]*?)\n\)/)?.[1]
    expect(block).toBeDefined()
    const backendCsp = [...block!.matchAll(/"([^"]*)"/g)].map((m) => m[1]).join('')
    expect(backendCsp).toBe(EXPECTED_CSP)
  })

  // The document and the hashed assets both come from app.frontend() behind
  // SecurityHeadersMiddleware, so the baseline set — Permissions-Policy and
  // nosniff included — reaches every SPA response, not only index.html. The old
  // nginx tier had dropped both on some locations.
  it('sends Permissions-Policy and nosniff on every SPA response', () => {
    const backend = readFrontendFile('../backend/src/tripl/middleware/security_headers.py')
    const baseline = backend.match(/headers = \{([\s\S]*?)\n {4}\}/)?.[1] ?? ''
    expect(baseline).toContain('"x-content-type-options": "nosniff"')
    expect(baseline).toContain(
      '"permissions-policy": "camera=(), microphone=(), geolocation=(), payment=()"',
    )

    const main = readFrontendFile('../backend/src/tripl/main.py')
    expect(main).toContain('app.add_middleware(SecurityHeadersMiddleware)')
    expect(main).toMatch(/app\.frontend\("\/", directory=settings\.frontend_dist_dir/)
  })
})
