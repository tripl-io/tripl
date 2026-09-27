import type { EventHealth, HealthComponent } from '@/types/health'

/** One component, applicable by default; tests override what they check. */
export function healthComponent(overrides: Partial<HealthComponent> & Pick<HealthComponent, 'key'>): HealthComponent {
  return {
    label: overrides.key,
    weight: 10,
    applies: true,
    excluded_reason: null,
    value: 1,
    effective_weight: 10,
    points: 10,
    detail: '',
    counts: {},
    ...overrides,
  }
}

/**
 * A shipped event with no scan covering it: drifts, signals and freshness are
 * excluded, and the other three are rescaled over their 60 points.
 */
export const RENORMALIZED_HEALTH: EventHealth = {
  event_id: 'event-1',
  event_type_id: 'type-1',
  name: 'checkout_completed',
  score: 71,
  grade: 'warning',
  renormalized: true,
  excluded: ['drifts', 'signals', 'freshness'],
  top_issue: '2 of 9 contract rules failing: amount (range), plan (enum)',
  components: [
    healthComponent({
      key: 'implemented_seen',
      label: 'Implemented & seen',
      weight: 25,
      value: 1,
      effective_weight: 41.7,
      points: 41.7,
      detail: 'Last seen 3d ago',
    }),
    healthComponent({
      key: 'contract',
      label: 'Contract',
      weight: 20,
      value: 0.778,
      effective_weight: 33.3,
      points: 25.9,
      detail: '2 of 9 contract rules failing: amount (range), plan (enum)',
      counts: { violated: 2, total: 9 },
    }),
    healthComponent({
      key: 'drifts',
      label: 'Drifts',
      weight: 15,
      applies: false,
      excluded_reason: 'Not covered by any scan',
      value: null,
      effective_weight: null,
      points: null,
    }),
    healthComponent({
      key: 'signals',
      label: 'Signals',
      weight: 15,
      applies: false,
      excluded_reason: 'Anomaly detection is off for its scans',
      value: null,
      effective_weight: null,
      points: null,
    }),
    healthComponent({
      key: 'freshness',
      label: 'Freshness',
      weight: 10,
      applies: false,
      excluded_reason: 'No scheduled source',
      value: null,
      effective_weight: null,
      points: null,
    }),
    healthComponent({
      key: 'documentation',
      label: 'Documentation',
      weight: 15,
      value: 0.5,
      effective_weight: 25,
      points: 12.5,
      detail: 'No owner',
    }),
  ],
}
