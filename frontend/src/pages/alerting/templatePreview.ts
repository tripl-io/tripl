/**
 * Example values for the preview: what a Telegram or Slack message
 * built from this template roughly looks like, without a round-trip. Unknown
 * variables stay as typed, so a typo is visible in the preview too.
 */
const PREVIEW_VALUES: Readonly<Record<string, string>> = {
  project_name: 'Demo shop',
  project_slug: 'demo-shop',
  channel: 'slack',
  destination_name: '#alerts',
  rule_name: 'Checkout drops',
  scan_name: 'Production events',
  matched_count: '2',
  items_count: '2',
  headline: '2 alerts · 1 down, 1 up · worst checkout_started down 42%',
  window_label: 'Sep 24, 09:00–10:00',
  ai_explanation_block: '',
  scope_name: 'checkout_started',
  scope_type: 'event',
  scope_label: 'Event',
  direction: 'drop',
  direction_label: 'down',
  direction_arrow: '▼',
  scope_link: 'checkout_started',
  actual_count: '580',
  expected_count: '1000',
  expected_basis: '',
  absolute_delta: '-420',
  percent_delta: '-42.0',
  percent_delta_label: '-42.0%',
  bucket: '2026-09-24 09:00',
  details_url: 'https://tripl.example/p/demo-shop/events/…',
  monitoring_url: 'https://tripl.example/p/demo-shop/monitoring/…',
  details_line: '\nDetails: https://tripl.example/p/demo-shop/events/…',
  monitoring_line: '\nMonitoring: https://tripl.example/p/demo-shop/monitoring/…',
  drift_field: '',
  drift_type: '',
  sample_value: '',
  drift_line: '',
  sparkline: '▅▆▇▆▅▂',
  sparkline_line: '\n▅▆▇▆▅▂',
  top_movers: 'ios −61%, android −12%',
  top_movers_line: '\nTop movers: ios −61%, android −12%',
}

const PREVIEW_ITEMS_TEXT = [
  '- Event checkout_started: down, actual=580, expected=1000, delta=-420 (-42.0%)',
  '- Event type Purchase: up, actual=320, expected=200, delta=120 (60.0%)',
].join('\n')

export function renderTemplatePreview(template: string): string {
  return template.replace(/\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g, (token, name: string) => {
    if (name === 'items_text') return PREVIEW_ITEMS_TEXT
    return PREVIEW_VALUES[name] ?? token
  })
}
