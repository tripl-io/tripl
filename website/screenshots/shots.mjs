// What capture.mjs photographs. Each shot is a page (`url`), optionally an
// action on it (`prepare`), and optionally an element to crop to (`clip`).
// `ctx` is the freshly generated demo project: `ctx.projectUrl('events')`,
// `ctx.events['Home Screen View'].id`, `ctx.branch`.
//
// A page that uses a shot names it as
//   ![What it shows](/img/screenshots/<id>.light.webp#gh-light-mode-only)
//   ![What it shows](/img/screenshots/<id>.dark.webp#gh-dark-mode-only)
// so the picture follows the reader's theme.

export const SHOTS = [
  {
    id: 'workspace',
    url: ctx => ctx.url('/o/default/workspace'),
  },
  {
    id: 'overview',
    url: ctx => ctx.projectUrl('overview'),
  },
  {
    id: 'events',
    url: ctx => ctx.projectUrl('events'),
  },
  {
    id: 'event-monitoring',
    url: ctx => ctx.projectUrl(`monitoring/event/${ctx.events['Home Screen View'].id}`),
  },
  {
    id: 'anomalies',
    url: ctx => ctx.projectUrl('anomalies'),
  },
  {
    id: 'alerting-inbox',
    url: ctx => ctx.projectUrl('alerting'),
  },
  {
    id: 'reconciliation',
    url: ctx => ctx.projectUrl('reconciliation'),
  },
  {
    id: 'branches',
    url: ctx => ctx.projectUrl(`branches/${ctx.branch.id}`),
  },
  {
    id: 'docs-note',
    url: ctx => ctx.projectUrl('docs/project/guides/checkout-funnel.md'),
  },
  {
    id: 'data-sources',
    url: ctx => ctx.url('/settings/data-sources'),
  },
  {
    // The demo as it greets a newcomer: banner, welcome panel, chapters.
    id: 'demo-welcome',
    showDemo: true,
    url: ctx => ctx.projectUrl('overview'),
  },
  {
    id: 'command-palette',
    url: ctx => ctx.projectUrl('events'),
    prepare: async (page, h) => {
      await h.key('Control+k')
      await page.keyboard.type('purchase')
      await h.settle()
    },
  },
  {
    id: 'event-edit',
    url: ctx => ctx.projectUrl(`events/screen-view/${ctx.events['Home Screen View'].id}/edit`),
  },
  {
    id: 'alert-rules',
    url: ctx => ctx.projectUrl('alerting'),
    prepare: (page, h) => h.click('Rules'),
  },
  {
    id: 'alert-rule-new',
    url: ctx => ctx.projectUrl('alerting'),
    prepare: async (page, h) => {
      await h.click('Rules')
      await h.click('Add rule')
      await page.keyboard.type('Checkout volume')
    },
  },
  {
    id: 'alert-destinations',
    url: ctx => ctx.projectUrl('alerting'),
    prepare: (page, h) => h.click('Destinations'),
  },
  {
    id: 'branch-new',
    url: ctx => ctx.projectUrl('branches'),
    prepare: async (page, h) => {
      await h.click('New branch')
      await page.keyboard.type('checkout/paywall-copy')
    },
  },
  {
    id: 'scan-detail',
    url: ctx => ctx.projectUrl(`scans/${ctx.scan.id}`),
  },
  {
    id: 'scan-new',
    url: ctx => ctx.projectUrl('scans'),
    prepare: (page, h) => h.click('New scan'),
  },
  {
    id: 'metrics',
    url: ctx => ctx.projectUrl('metrics'),
  },
  {
    id: 'data-source-add',
    url: ctx => ctx.url('/settings/data-sources'),
    prepare: async (page, h) => {
      await h.click('Add connection')
      await page.keyboard.type('Production ClickHouse')
    },
  },
  {
    id: 'invitations',
    url: ctx => ctx.url('/settings/invitations'),
    prepare: (page, h) => h.type('input[type="email"]', 'alex@acme.example'),
  },
  {
    id: 'project-access',
    url: ctx => ctx.url('/settings/project/members'),
  },
  {
    id: 'api-key-create',
    url: ctx => ctx.url('/settings/api-keys'),
    prepare: async (page, h) => {
      await h.click('Create key')
      await page.keyboard.type('claude-agent')
    },
  },
  {
    id: 'docs-translate',
    url: ctx => ctx.projectUrl('docs/project/guides/checkout-funnel.md'),
    prepare: async (page, h) => {
      await h.click('Translate with AI')
      await page.keyboard.type('German')
    },
  },
]
