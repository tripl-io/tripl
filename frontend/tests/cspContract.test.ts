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

  it('keeps the standalone nginx CSP aligned with the frontend resources', () => {
    const indexHtml = readFrontendFile('index.html')
    const nginxConfig = readFrontendFile('nginx.conf')
    const nginxCsp = nginxConfig.match(
      /add_header Content-Security-Policy "([^"]+)" always;/,
    )?.[1]

    expect(nginxCsp).toBe(EXPECTED_CSP)
    // Nothing on the page points at a font host the CSP does not name.
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

  // Two deploy shapes serve the SPA: the API's own static handler and the
  // standalone nginx image. They drifted once — nginx had no frame-src, so the
  // Figma embed was blocked there only (#194).
  it('matches the backend default CSP exactly', () => {
    const backend = readFrontendFile('../backend/src/tripl/middleware/security_headers.py')
    const block = backend.match(/_DEFAULT_SPA_CSP = \(([\s\S]*?)\n\)/)?.[1]
    expect(block).toBeDefined()
    const backendCsp = [...block!.matchAll(/"([^"]*)"/g)].map((m) => m[1]).join('')
    expect(backendCsp).toBe(EXPECTED_CSP)
  })

  // The same two deploy shapes: the standalone nginx document went out without
  // the Permissions-Policy the API's own static handler sends.
  it('sends the backend Permissions-Policy on the SPA document', () => {
    const backend = readFrontendFile('../backend/src/tripl/middleware/security_headers.py')
    const backendPolicy = backend.match(/"permissions-policy": "([^"]+)"/)?.[1]
    expect(backendPolicy).toBeDefined()

    const nginxConfig = readFrontendFile('nginx.conf')
    const documentBlock = nginxConfig.match(/location \/ \{([\s\S]*?)\n {8}\}/)?.[1] ?? ''
    const nginxPolicy = documentBlock.match(/add_header Permissions-Policy "([^"]+)" always;/)?.[1]
    expect(nginxPolicy).toBe(backendPolicy)
  })

  it('lets the API accept an event photo as large as the backend allows', () => {
    const nginxConfig = readFrontendFile('nginx.conf')
    const apiBlock = nginxConfig.match(/location \/api\/ \{([\s\S]*?)\n {8}\}/)?.[1] ?? ''
    // photo_max_size_mb = 10 in backend/src/tripl/config.py; nginx defaults to 1m.
    const limit = apiBlock.match(/client_max_body_size (\d+)m;/)?.[1]
    expect(Number(limit)).toBeGreaterThanOrEqual(10)
  })

  it('sends nosniff on hashed assets too, not only on the document', () => {
    const nginxConfig = readFrontendFile('nginx.conf')
    const assetsBlock = nginxConfig.match(/location \/assets\/ \{([\s\S]*?)\n {8}\}/)?.[1] ?? ''
    expect(assetsBlock).toContain('add_header X-Content-Type-Options "nosniff" always;')
  })
})
