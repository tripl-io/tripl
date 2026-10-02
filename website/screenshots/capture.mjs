// Captures the docs site's screenshots from a running tripl.
//
// It signs in (registering the account on a fresh instance), generates a new
// demo project so every run starts from the same freshly seeded state, adds
// the few things the demo does not have (notes in Docs), then visits each shot
// in shots.mjs once per theme and writes
// ../static/img/screenshots/<id>.<theme>.webp.
//
//   TRIPL_URL       the app, e.g. http://localhost:5173 (the Vite dev server)
//   TRIPL_EMAIL     an owner account; registered if the instance has none
//   TRIPL_PASSWORD
//   CHROME_PATH     a Chrome or Chromium binary (puppeteer-core brings none)
//   THEMES          light,dark (default: both)
//   ONLY            comma-separated shot ids, to retake a few
//
// See README.md for the whole routine.

import { mkdir } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import puppeteer from 'puppeteer-core'
import { SHOTS } from './shots.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const OUT = path.resolve(here, '../static/img/screenshots')
const BASE = (process.env.TRIPL_URL ?? 'http://localhost:5173').replace(/\/$/, '')
const EMAIL = process.env.TRIPL_EMAIL ?? 'maria@acme.example'
const PASSWORD = process.env.TRIPL_PASSWORD ?? 'DocsShots-2026!x'
const NAME = 'Maria Chen'
const THEMES = (process.env.THEMES ?? 'light,dark').split(',').map(t => t.trim()).filter(Boolean)
const ONLY = process.env.ONLY ? new Set(process.env.ONLY.split(',').map(s => s.trim())) : null
const VIEWPORT = { width: 1440, height: 900, deviceScaleFactor: 1 }

// The demo's own chrome (the synthetic-data banner, the welcome panel) is
// hidden unless a shot asks for it: the docs show the product as a real project
// looks. Toasts are hidden too, and motion is off so nothing is caught mid-way.
const CLEAN_CSS = `
  [data-demo-banner], section[aria-labelledby="demo-welcome-heading"],
  [role="note"][aria-label="Demo hint"] { display: none !important; }
  [data-coach-target] { box-shadow: none !important; outline: none !important; }
`
const STILL_CSS = `
  [data-sonner-toaster] { display: none !important; }
  *, *::before, *::after { transition: none !important; animation-duration: 0s !important;
    animation-delay: 0s !important; caret-color: transparent !important; }
`

function chromePath() {
  const candidates = [
    process.env.CHROME_PATH,
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
    '/usr/bin/google-chrome',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ].filter(Boolean)
  const found = candidates.find(p => existsSync(p))
  if (!found) throw new Error('No Chrome found: set CHROME_PATH')
  return found
}

async function api(page, method, url, body) {
  return page.evaluate(
    async (m, u, b) => {
      const r = await fetch(u, {
        method: m,
        credentials: 'include',
        headers: { 'content-type': 'application/json' },
        body: b === undefined ? undefined : JSON.stringify(b),
      })
      const text = await r.text()
      let json = null
      try {
        json = JSON.parse(text)
      } catch {
        // not JSON
      }
      return { status: r.status, json, text }
    },
    method,
    `/api/v1${url}`,
    body,
  )
}

async function must(page, method, url, body) {
  const r = await api(page, method, url, body)
  if (r.status >= 300) throw new Error(`${method} ${url} -> ${r.status}: ${r.text.slice(0, 300)}`)
  return r.json
}

async function signIn(page) {
  await page.goto(`${BASE}/auth`, { waitUntil: 'domcontentloaded' })
  let r = await api(page, 'POST', '/auth/login', { email: EMAIL, password: PASSWORD })
  if (r.status === 401 || r.status === 400) {
    await must(page, 'POST', '/auth/register', { email: EMAIL, password: PASSWORD, name: NAME })
    r = await api(page, 'POST', '/auth/login', { email: EMAIL, password: PASSWORD })
  }
  if (r.status >= 300) throw new Error(`Sign-in failed: ${r.status} ${r.text.slice(0, 200)}`)
}

/** Waits until nothing on the page says it is loading, then a beat more. */
async function settle(page, { timeout = 20_000 } = {}) {
  const started = Date.now()
  let quietSince = 0
  while (Date.now() - started < timeout) {
    const busy = await page.evaluate(
      () => document.querySelectorAll('.animate-pulse, [aria-busy="true"], .animate-spin').length,
    )
    if (busy === 0) {
      if (!quietSince) quietSince = Date.now()
      if (Date.now() - quietSince > 700) break
    } else {
      quietSince = 0
    }
    await new Promise(r => setTimeout(r, 150))
  }
  await new Promise(r => setTimeout(r, 400))
}

function helpers(page) {
  return {
    /**
     * Clicks the first button, link, tab or menu item named `name`, with the
     * mouse: Radix tabs and menus answer a pointer press, not `el.click()`.
     */
    async click(name, { within = 'body', exact = true } = {}) {
      const found = await page.evaluate(
        (n, scope, ex) => {
          document.querySelector('[data-shot-target]')?.removeAttribute('data-shot-target')
          const root = document.querySelector(scope) ?? document.body
          const items = [
            ...root.querySelectorAll('button, a, [role="tab"], [role="menuitem"], [role="option"]'),
          ]
          const label = el =>
            (el.getAttribute('aria-label') ?? el.textContent ?? '').replace(/\s+/g, ' ').trim()
          const hit = items.find(el => (ex ? label(el) === n : label(el).startsWith(n)))
          hit?.setAttribute('data-shot-target', '')
          return Boolean(hit)
        },
        name,
        within,
        exact,
      )
      if (!found) throw new Error(`Nothing to click named "${name}"`)
      await page.click('[data-shot-target]')
      await settle(page)
    },
    /** Types into the field matching `selector`. */
    async type(selector, text) {
      await page.waitForSelector(selector, { timeout: 10_000 })
      await page.click(selector)
      await page.type(selector, text)
      await settle(page)
    },
    /** Presses a key combination such as `Control+k`. */
    async key(combo) {
      const keys = combo.split('+')
      for (const k of keys) await page.keyboard.down(k)
      for (const k of [...keys].reverse()) await page.keyboard.up(k)
      await settle(page)
    },
    settle: () => settle(page),
  }
}

const NOTES = {
  'guides/checkout-funnel.md': [
    '---',
    'title: How we measure the checkout funnel',
    'description: The events, the order they fire in, and the traps.',
    'tags: [checkout, funnel]',
    '---',
    '# How we measure the checkout funnel',
    '',
    'The funnel starts at [[event:Paywall View]] and ends at [[event:Purchase Completed]].',
    'A user who taps [[event:Buy Button Click]] but never completes counts as dropped.',
    '',
    '## Steps',
    '',
    '1. Paywall View',
    '2. Buy Button Click',
    '3. Purchase Completed, or Purchase Failed',
    '',
    '## Traps',
    '',
    '- A failed payment that is retried fires `Purchase Failed` once per attempt: count users, not events.',
    '- Amounts are in cents.',
    '',
    '```sql',
    'select count(distinct user_id)',
    'from events',
    "where name = 'Purchase Completed'",
    '```',
    '',
  ].join('\n'),
  'guides/onboarding.md': '# Onboarding steps\n\nWhat each onboarding screen is for, and which event it sends.\n',
  'runbooks/when-volume-drops.md':
    '# When volume drops\n\nCheck the release notes first, then the platform breakdown.\n',
  'SKILL.md': [
    '---',
    'name: tracking-plan',
    'description: How agents should read and change this tracking plan.',
    'audience: agent',
    '---',
    '# Tracking plan skill',
    '',
    'Read the plan through MCP before proposing events.',
    '',
  ].join('\n'),
}

async function seed(page) {
  // One demo at a time, so the new one is plainly "Demo Project" and not
  // "Demo Project 2". Only demo projects are touched.
  for (const old of await must(page, 'GET', '/projects')) {
    if (old.is_demo) await must(page, 'DELETE', `/projects/demo/${old.slug}`)
  }
  const project = await must(page, 'POST', '/projects/demo', {})
  const slug = project.slug
  const events = await must(page, 'GET', `/projects/${slug}/events?limit=100`)
  const byName = Object.fromEntries((events.items ?? events).map(e => [e.name, e]))
  const branches = await must(page, 'GET', `/projects/${slug}/branches`)
  const scans = await must(page, 'GET', `/projects/${slug}/scans`)
  for (const [p, content] of Object.entries(NOTES)) {
    await must(page, 'PUT', `/projects/${slug}/docs/file?scope=project&path=${encodeURIComponent(p)}`, {
      content,
    })
  }
  return {
    slug,
    events: byName,
    branch: (branches.items ?? branches)[0],
    scan: (scans.items ?? scans)[0],
    projectUrl: rest => `${BASE}/o/default/p/${slug}/${rest}`,
    url: rest => `${BASE}${rest}`,
  }
}

async function main() {
  await mkdir(OUT, { recursive: true })
  const browser = await puppeteer.launch({
    executablePath: chromePath(),
    headless: true,
    args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage', '--hide-scrollbars'],
    defaultViewport: VIEWPORT,
  })
  try {
    const setup = await browser.newPage()
    await signIn(setup)
    const ctx = await seed(setup)
    console.log(`demo project ${ctx.slug}`)
    const shots = SHOTS.filter(s => !ONLY || ONLY.has(s.id))
    let failed = 0
    for (const theme of THEMES) {
      for (const shot of shots) {
        const page = await browser.newPage()
        await page.setViewport({ ...VIEWPORT, ...(shot.viewport ?? {}) })
        await page.evaluateOnNewDocument(
          (t, css) => {
            localStorage.setItem('tripl-ui-theme', t)
            const add = () => {
              const s = document.createElement('style')
              s.textContent = css
              document.head.appendChild(s)
            }
            if (document.head) add()
            else document.addEventListener('DOMContentLoaded', add)
          },
          theme,
          (shot.showDemo ? '' : CLEAN_CSS) + STILL_CSS,
        )
        try {
          await page.goto(shot.url(ctx), { waitUntil: 'domcontentloaded' })
          await settle(page)
          if (shot.prepare) await shot.prepare(page, helpers(page), ctx)
          const file = path.join(OUT, `${shot.id}.${theme}.webp`)
          const target = shot.clip ? await page.$(shot.clip) : null
          if (shot.clip && !target) throw new Error(`No element for clip ${shot.clip}`)
          await (target ?? page).screenshot({ path: file, type: 'webp', quality: 82 })
          console.log(`ok   ${shot.id}.${theme}`)
        } catch (err) {
          failed += 1
          console.log(`FAIL ${shot.id}.${theme}: ${err.message}`)
        } finally {
          await page.close()
        }
      }
    }
    if (failed) process.exitCode = 1
  } finally {
    await browser.close()
  }
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
