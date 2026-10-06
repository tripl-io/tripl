import { randomUUID } from 'node:crypto'
import type {
  Browser,
  BrowserContext,
  Locator,
  Page,
  PageAssertionsToHaveScreenshotOptions,
} from '@playwright/test'
import { expect, generateDemo, test } from './fixtures'

/**
 * The demo's layout, in a real browser: what unit tests could only assert as
 * class names (`min-w-0`, `overflow-x-hidden`, `bottom-[68px]`). The tour, a
 * chapter and its coach card must fit the screen at desktop and phone
 * widths with no sideways scroll, and the banner, the scenario strip, the tour
 * and the coach card are compared with their screenshots in light and dark.
 *
 * One demo serves the whole file: generating it runs the worker for most of a
 * minute. The chapter progress and the theme live in the browser's storage, so
 * each test starts from a fresh context on the same signed-in session.
 */

const DESKTOP = { width: 1440, height: 900 }
const PHONE = { width: 375, height: 812 }

type Theme = 'light' | 'dark'
type Box = { x: number; y: number; width: number; height: number }

let session: Awaited<ReturnType<BrowserContext['storageState']>>
let slug: string

test.beforeAll(async ({ browser }) => {
  const context = await browser.newContext({ viewport: DESKTOP })
  const page = await context.newPage()
  const resp = await page.request.post('/api/v1/auth/register', {
    data: {
      email: `e2e-${randomUUID().slice(0, 8)}@example.com`,
      password: `E2e-${randomUUID()}`,
      name: 'E2E Layout',
    },
  })
  expect(resp.status(), await resp.text()).toBe(201)
  slug = await generateDemo(page)
  session = await context.storageState()
  await context.close()
})

/** The demo's overview in a fresh context: no tour step or chapter progress yet. */
async function openDemo(browser: Browser, viewport: typeof DESKTOP, theme: Theme = 'dark'): Promise<Page> {
  const context = await browser.newContext({ viewport, storageState: session, reducedMotion: 'reduce' })
  await context.addInitScript(`localStorage.setItem('tripl-ui-theme', ${JSON.stringify(theme)})`)
  const page = await context.newPage()
  await page.goto(`/p/${slug}/overview`)
  // On a phone the banner is a pill until it is opened: wait for any of it.
  await expect(page.locator('[data-demo-banner]').getByRole('button').filter({ visible: true }).first()).toBeVisible({ timeout: 60_000 })
  return page
}

/** On a phone the banner's actions sit in a panel its pill opens. */
async function openBannerPanel(page: Page): Promise<void> {
  const pill = page.locator('[data-demo-banner]').getByRole('button', { name: 'Demo workspace tools' })
  if ((await pill.isVisible()) && (await pill.getAttribute('aria-expanded')) !== 'true') await pill.click()
}

/** The banner's "Tour & chapters". */
async function openTour(page: Page): Promise<Locator> {
  await openBannerPanel(page)
  await page.locator('[data-demo-banner]').getByRole('button', { name: 'Tour & chapters' }).click()
  const tour = page.getByRole('dialog', { name: 'Product tour' })
  await expect(tour).toBeVisible()
  return tour
}

/**
 * Start the first chapter from the tour and wait for its coach card to settle.
 * The chapter lands on Scans and may go on to the scan it coaches; the card is
 * docked to the viewport while its Run button sits in a table row, and a
 * popover beside it on the scan's own page.
 */
async function startFirstChapter(page: Page, tour: Locator): Promise<Locator> {
  await tour.getByRole('list', { name: 'Scenario chapters' }).getByRole('button').first().click()
  await expect(page).toHaveURL(/\/scans(\/[0-9a-f-]+)?$/, { timeout: 60_000 })
  const card = page.getByRole('note', { name: 'Demo hint' })
  await expect(card).toContainText('Run a scan to pull fresh volume from the demo warehouse.', { timeout: 60_000 })
  // Settled: the same page and the same box twice, a second apart.
  let last = ''
  await expect
    .poll(
      async () => {
        const now = JSON.stringify([page.url(), await card.getAttribute('data-coach-docked'), await card.boundingBox()])
        const same = now === last
        last = now
        return same
      },
      { intervals: [1_000], timeout: 30_000 },
    )
    .toBe(true)
  return card
}

async function boxOf(locator: Locator, what: string): Promise<Box> {
  const box = await locator.boundingBox()
  if (!box) throw new Error(`${what} is not rendered`)
  return box
}

async function expectNoSidewaysPageScroll(page: Page): Promise<void> {
  const overflow = await page.locator('html').evaluate((el) => el.scrollWidth - el.clientWidth)
  expect(overflow, 'the page scrolls sideways').toBeLessThanOrEqual(0)
}

async function expectInsideViewport(page: Page, locator: Locator, what: string): Promise<void> {
  const box = await boxOf(locator, what)
  const viewport = page.viewportSize()!
  expect(box.x, `${what} starts off the left edge`).toBeGreaterThanOrEqual(0)
  expect(box.y, `${what} starts above the top edge`).toBeGreaterThanOrEqual(0)
  expect(box.x + box.width, `${what} runs off the right edge`).toBeLessThanOrEqual(viewport.width)
  expect(box.y + box.height, `${what} runs off the bottom edge`).toBeLessThanOrEqual(viewport.height)
}

/** Content wider than its box scrolls or is clipped instead of fitting. */
async function expectNoSidewaysOverflow(locator: Locator, what: string): Promise<void> {
  const overflow = await locator.evaluate((el) => el.scrollWidth - el.clientWidth)
  expect(overflow, `${what} is wider than its box`).toBeLessThanOrEqual(0)
}

function overlaps(a: Box, b: Box): boolean {
  return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height
}

for (const [name, viewport] of [
  ['desktop', DESKTOP],
  ['phone', PHONE],
] as const) {
  test(`the tour, a chapter and its coach card fit the screen (${name}, ${viewport.width}px)`, async ({
    browser,
  }) => {
    const page = await openDemo(browser, viewport)
    await expectNoSidewaysPageScroll(page)

    // The phone panel wraps its actions instead of pushing them off-screen.
    await openBannerPanel(page)
    const actions = await page.locator('[data-demo-banner]').getByRole('button').all()
    expect(actions.length).toBeGreaterThan(1)
    for (const button of actions) {
      if (await button.isVisible()) await expectInsideViewport(page, button, 'a banner action')
    }

    const tour = await openTour(page)
    await expectInsideViewport(page, tour, 'the tour')
    await expectNoSidewaysOverflow(tour, 'the tour')
    const tourBox = await boxOf(tour, 'the tour')
    const rows = await tour.getByRole('list', { name: 'Scenario chapters' }).getByRole('button').all()
    expect(rows.length).toBeGreaterThan(1)
    for (const row of rows) {
      const box = await boxOf(row, 'a chapter row')
      expect(box.x + box.width, 'a chapter row runs past the tour').toBeLessThanOrEqual(tourBox.x + tourBox.width)
    }

    const card = await startFirstChapter(page, tour)
    await expectInsideViewport(page, card, 'the coach card')
    await expectNoSidewaysPageScroll(page)
    // Docked or not, it does not take a table cell's right-align.
    expect(await card.evaluate((el) => el.ownerDocument.defaultView?.getComputedStyle(el).textAlign)).toBe('left')
    // Its own buttons take the clicks, not something stacked over them.
    await card.getByRole('button', { name: 'Hide hints' }).click({ trial: true })
    if ((await card.getAttribute('data-coach-docked')) === 'true') {
      const url = page.url()
      await card.getByRole('button', { name: 'Collapse demo hint' }).click()
      await card.getByRole('button', { name: 'Expand demo hint' }).click()
      await expect(card.getByRole('button', { name: 'Collapse demo hint' })).toBeVisible()
      // The card sits in a clickable row; its clicks used to open the scan.
      expect(page.url()).toBe(url)
      // A phone always gets a bottom sheet.
      if (viewport === PHONE) await expect(card).toHaveAttribute('data-coach-edge', 'bottom')
    }
    if (viewport === DESKTOP) {
      // Clear of the sidebar's Appearance button, where the tweaks FAB was.
      const appearance = page.getByRole('button', { name: 'Appearance' })
      expect(overlaps(await boxOf(card, 'the coach card'), await boxOf(appearance, 'Appearance'))).toBe(false)
    }
    await page.context().close()
  })
}

for (const theme of ['light', 'dark'] as const) {
  test(`the demo bar, tour and coach card look as they did (${theme})`, async ({ browser }) => {
    const page = await openDemo(browser, DESKTOP, theme)
    const banner = page.locator('[data-demo-banner]')
    // Soft, so one run reports (and with the label writes) every shot.
    const shot: PageAssertionsToHaveScreenshotOptions = { animations: 'disabled', caret: 'hide', maxDiffPixelRatio: 0.01 }

    // The freshness stamp is the one line that reads the clock. A mask paints
    // over its spot in every screenshot, the tour's included, so only here.
    await expect.soft(banner).toHaveScreenshot(`banner-${theme}.png`, {
      ...shot,
      mask: [banner.getByText(/^(freshly seeded|updated )/)],
    })

    const tour = await openTour(page)
    await expect.soft(tour).toHaveScreenshot(`tour-${theme}.png`, shot)

    const card = await startFirstChapter(page, tour)
    await expect.soft(banner.getByRole('region', { name: 'Demo scenario' })).toHaveScreenshot(`strip-${theme}.png`, shot)
    await expect.soft(card).toHaveScreenshot(`coach-card-${theme}.png`, shot)
    await page.context().close()
  })
}
