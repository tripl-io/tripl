import type { ProjectTemplateSummary } from '@/types/projectTemplates'

/**
 * The e-commerce template as `GET /project-templates` returns it (F21): the
 * branch name, counts and suggestion shapes match
 * backend/src/tripl/services/project_templates/ecommerce.py, so tests check the
 * contract the backend really produces. Suggestion lists are trimmed to one of
 * each; `counts` still reports the backend's totals.
 */
export function ecommerceTemplate(
  overrides: Partial<ProjectTemplateSummary> = {},
): ProjectTemplateSummary {
  return {
    id: 'ecommerce',
    version: 1,
    name: 'E-commerce',
    description:
      'Online store funnel: product discovery, cart, checkout and orders, including refunds.',
    branch_name: 'template/ecommerce',
    counts: {
      event_types: 5,
      fields: 24,
      events: 9,
      variables: 4,
      metric_suggestions: 5,
      alert_suggestions: 3,
    },
    event_type_names: ['page_view', 'product', 'cart', 'checkout', 'order'],
    metric_suggestions: [
      {
        name: 'checkout_conversion',
        display_name: 'Checkout conversion',
        description: 'Share of started checkouts that end in a completed order.',
        kind: 'event_composition',
        composition: 'ratio',
        numerator_event: 'order_completed',
        denominator_event: 'checkout_started',
        needs: 'scan',
      },
    ],
    alert_suggestions: [
      {
        name: 'core_funnel_volume_drop',
        description: 'Volume drops on core funnel events (product views, checkouts, orders).',
        needs: 'alert_destination',
      },
    ],
    ...overrides,
  }
}
