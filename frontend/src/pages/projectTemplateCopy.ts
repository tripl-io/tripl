import type {
  ProjectTemplateCounts,
  ProjectTemplateMetricNeeds,
  ProjectTemplateSummary,
} from '@/types/projectTemplates'

/** Copy shared by the New project dialog's template picker and its tests (F21). */

export const BLANK_PROJECT_LABEL = 'Blank project'
export const SUGGESTIONS_NOTE =
  'Suggestions only. Created after you connect a data source / alert destination — not added automatically.'

function plural(count: number, one: string, many: string) {
  return `${count} ${count === 1 ? one : many}`
}

/** "12 events · 5 event types · 4 variables" */
export function formatTemplateCounts(counts: ProjectTemplateCounts): string {
  return [
    plural(counts.events, 'event', 'events'),
    plural(counts.event_types, 'event type', 'event types'),
    plural(counts.variables, 'variable', 'variables'),
  ].join(' · ')
}

/** The card's detail line: "9 events · 5 event types · 4 variables · version 1" */
export function formatTemplateMeta(
  template: Pick<ProjectTemplateSummary, 'counts' | 'version'>,
): string {
  return `${formatTemplateCounts(template.counts)} · version ${template.version}`
}

export const NEEDS_LABEL: Record<ProjectTemplateMetricNeeds, string> = {
  scan: 'needs a scan',
  data_source: 'needs a data source',
}

/** Under the picker once a template is chosen (F21). */
export const TEMPLATE_HINT =
  "The template's plan opens as a draft branch for review; the project's main plan stays empty until you merge it."
