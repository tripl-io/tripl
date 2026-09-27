import type { components } from './api.gen'
import type { Project } from './projects'

/**
 * Project templates (F21, #274). Derived from the generated OpenAPI schema so
 * the gen:api drift check covers them. A template's plan lands on a
 * reviewable draft branch; the project's main plan stays empty until that
 * branch is merged.
 */

type Schemas = components['schemas']

export type ProjectTemplateCounts = Schemas['ProjectTemplateCounts']

/** A starter metric the template suggests. Informational only: the backend
 *  never creates a metric from it. `kind` is the metric-definition kind to
 *  create; `composition` and the event names are set only for
 *  `event_composition`. */
export type ProjectTemplateMetricSuggestion = Schemas['ProjectTemplateMetricSuggestionOut']

export type ProjectTemplateMetricKind = ProjectTemplateMetricSuggestion['kind']

/** What a starter metric waits for before anyone can create it. */
export type ProjectTemplateMetricNeeds = ProjectTemplateMetricSuggestion['needs']

/** A starter alert rule the template suggests. Informational only: an alert
 *  rule needs a destination, which the template cannot supply. */
export type ProjectTemplateAlertSuggestion = Schemas['ProjectTemplateAlertSuggestionOut']

/** `GET /project-templates` item, in the backend's display order. */
export type ProjectTemplateSummary = Schemas['ProjectTemplateSummary']

/** `POST /projects` body. `template_id` is omitted (or null) for a blank project. */
export type ProjectCreateInput = Pick<Schemas['ProjectCreate'], 'name' | 'slug'> &
  Partial<Pick<Schemas['ProjectCreate'], 'description' | 'template_id'>>

/** `POST /projects` 201 response: the project plus the template's draft
 *  branch, null when no template was given. */
export type ProjectCreateResult = Project &
  Required<Pick<Schemas['ProjectCreateResponse'], 'template_branch_id'>>
