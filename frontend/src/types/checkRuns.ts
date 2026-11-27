// Uploaded `tripl check` runs (`tripl check --upload`), aliased from the
// committed backend OpenAPI (`api.gen.ts`): the history, the repositories and
// check directories it is filtered by, "used in code" on one event, and the
// project's link template.
import type { components } from './api.gen'

type Schemas = components['schemas']

export type CheckRun = Schemas['CheckRunResponse']
export type CheckRunCounts = Schemas['CheckRunCounts']
export type CheckRunList = Schemas['CheckRunListResponse']
export type CheckRunDirectory = Schemas['CheckRunDirectory']
export type CheckRunDirectoryList = Schemas['CheckRunDirectoryListResponse']
export type CheckRunCallLocation = Schemas['CheckRunCallLocation']
export type EventCodeUsageRepository = Schemas['EventCodeUsageRepository']
export type EventCodeUsage = Schemas['EventCodeUsageResponse']
export type CheckRunSettings = Schemas['CheckRunSettings']
export type CheckRunSettingsUpdate = Schemas['CheckRunSettingsUpdate']
