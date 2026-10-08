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
 * chapter and its demo guide must fit the screen at desktop and phone
 * widths with no sideways scroll, and the banner, the scenario strip, the tour
 * and the guide are compared with their screenshots in light and dark.
 *
 * One demo serves the whole file: generating it runs the worker for most of a
 * minute. The chapter progress and the theme live in the browser's storage, so
 * each test starts from a fresh context on the same signed-in session.
 */

const DESKTOP = { width: 1440, height: 900 }
const LAPTOP = { width: 1280, height: 800 }
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

const RUN_SCAN_INSTRUCTION = 'Run a scan to pull fresh volume from the demo warehouse.'

/** The control the first chapter's coach mark rings: Run, on the list or on the scan. */
function coachTarget(page: Page): Locator {
  return page.locator('[data-coach-target="live-loop/run-scan"]').first()
}

/**
 * Start the first chapter from the tour and wait for its demo guide to settle.
 * The chapter lands on Scans and may go on to the scan it coaches. The guide
 * waited for is the coach mark's, beside the ring on Run: until the page's
 * rows land, the guide host speaks for the step with the same words.
 */
async function startFirstChapter(page: Page, tour: Locator): Promise<Locator> {
  await tour.getByRole('list', { name: 'Scenario chapters' }).getByRole('button').first().click()
  await expect(page).toHaveURL(/\/scans(\/[0-9a-f-]+)?$/, { timeout: 60_000 })
  await expect(coachTarget(page)).toBeVisible({ timeout: 60_000 })
  // One guide: the host keeps quiet while a mark speaks.
  const guide = page.locator('[data-demo-guide]')
  await expect(guide).toHaveCount(1)
  await expect(guide).toContainText(RUN_SCAN_INSTRUCTION)
  // Settled: the same page, corner and box twice, a second apart.
  let last = ''
  await expect
    .poll(
      async () => {
        const now = JSON.stringify([page.url(), await guide.getAttribute('data-guide-corner'), await guide.boundingBox()])
        const same = now === last
        last = now
        return same
      },
      { intervals: [1_000], timeout: 30_000 },
    )
    .toBe(true)
  return guide
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
  test(`the tour, a chapter and its demo guide fit the screen (${name}, ${viewport.width}px)`, async ({
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

    const guide = await startFirstChapter(page, tour)
    await expectInsideViewport(page, guide, 'the demo guide')
    await expectNoSidewaysPageScroll(page)
    // In a corner, off the control it points at…
    expect(
      overlaps(await boxOf(guide, 'the demo guide'), await boxOf(coachTarget(page), 'the coached control')),
      'the guide covers the control it coaches',
    ).toBe(false)
    // …which carries the ring, and the tag saying what to do there.
    await expect(page.locator('.coach-ring')).toBeVisible()
    await expect(page.locator('[data-coach-tag]')).toHaveText('Click here')
    // Its text does not take a table cell's right-align.
    expect(
      await guide
        .getByText(RUN_SCAN_INSTRUCTION)
        .evaluate((el) => el.ownerDocument.defaultView?.getComputedStyle(el).textAlign),
    ).toBe('left')
    // Its own buttons take the clicks, not something stacked over them.
    await guide.getByRole('button', { name: 'Hide hints' }).click({ trial: true })
    const url = page.url()
    await guide.getByRole('button', { name: 'Minimise the demo guide' }).click()
    await guide.getByRole('button', { name: /^Show the demo guide/ }).click()
    await expect(guide.getByRole('button', { name: 'Minimise the demo guide' })).toBeVisible()
    // The coached Run sits in a clickable row; a click on the old card opened the scan.
    expect(page.url()).toBe(url)
    // Hidden hints leave the guide's face in its corner: the way back to them,
    // where testers used to scroll up to the strip to find one.
    await guide.getByRole('button', { name: 'Hide hints' }).click()
    const face = page.locator('[data-demo-guide][data-guide-mode="face"]')
    await expect(face).toBeVisible()
    await expect(page.locator('.coach-ring')).toHaveCount(0)
    await expectInsideViewport(page, face, 'the hidden hints’ face')
    await face.getByRole('button', { name: /^Show demo hints/ }).click()
    await expect(guide).toContainText(RUN_SCAN_INSTRUCTION)
    await expect(page.locator('.coach-ring')).toBeVisible()
    if (viewport === PHONE) {
      // A phone gets the screen's width, less a 12px gutter on each side.
      const width = (await boxOf(guide, 'the demo guide')).width
      expect(Math.abs(width - (viewport.width - 24)), 'the guide is not the width of the screen').toBeLessThanOrEqual(1)
    }
    if (viewport === DESKTOP) {
      const guideBox = await boxOf(guide, 'the demo guide')
      // Clear of the sidebar's Appearance button, where the tweaks FAB was…
      const appearance = page.getByRole('button', { name: 'Appearance' })
      expect(overlaps(guideBox, await boxOf(appearance, 'Appearance'))).toBe(false)
      // …and of the banner, whose controls put the coaching away (#251).
      expect(overlaps(guideBox, await boxOf(page.locator('[data-demo-banner]'), 'the banner'))).toBe(false)
    }
    await page.context().close()
  })
}

test('beside a wide dialog on a 1280px screen the demo guide keeps its words', async ({ browser }) => {
  const page = await openDemo(browser, LAPTOP)
  await startFirstChapter(page, await openTour(page))
  // A property's editor: 896px wide, which leaves 192px of screen either side.
  // The guide folded to its face there, the step's words gone.
  await page.goto(`/p/${slug}/variables`)
  await page.getByRole('button', { name: /^Edit property / }).first().click()
  const dialog = page.getByRole('dialog', { name: /^Edit: / })
  await expect(dialog).toBeVisible()
  const guide = page.locator('[data-demo-guide][data-guide-mode="narrow"]')
  await expect(guide).toContainText(RUN_SCAN_INSTRUCTION)
  await expectInsideViewport(page, guide, 'the narrow demo guide')
  await expectNoSidewaysOverflow(guide, 'the narrow demo guide')
  expect(
    overlaps(await boxOf(guide, 'the narrow demo guide'), await boxOf(dialog, 'the dialog')),
    'the guide covers the dialog',
  ).toBe(false)
  await page.context().close()
})

for (const theme of ['light', 'dark'] as const) {
  test(`the demo bar, tour and demo guide look as they did (${theme})`, async ({ browser }) => {
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

    const guide = await startFirstChapter(page, tour)
    await expect.soft(banner.getByRole('region', { name: 'Demo scenario' })).toHaveScreenshot(`strip-${theme}.png`, shot)
    // The name is the coach card's, which the guide replaced.
    await expect.soft(guide).toHaveScreenshot(`coach-card-${theme}.png`, shot)
    await page.context().close()
  })
}
