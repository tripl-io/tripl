import type { components } from './api.gen'
import type { ScanDryRunResponse } from './scans'
import type {
  ColumnSchema,
  DataSourceSchemaResponse,
  TableSchema,
} from './dataSourceSchema'
import type {
  ConnectionSettingsResponse,
  DataSource,
  DataSourceScanRef,
} from './dataSources'
import type {
  DependenciesResponse,
  DependencyEdge,
  DependencyEntity,
  ImpactChange,
  ImpactItem,
  ImpactResponse,
} from './dependencies'
import type {
  DocBacklinkItem,
  DocBacklinksResponse,
  DocBundle,
  DocBundleFile,
  DocFolderDeleteResponse,
  DocImportRequest,
  DocImportResult,
  DocLanguageDefaults,
  DocLinkResolution,
  DocLinkSuggestion,
  DocLinkSuggestionsResponse,
  DocMoveRequest,
  DocMoveResponse,
  DocRevisionListResponse,
  DocRevisionSummary,
  DocSearchHit,
  DocSearchResponse,
  DocSharingUpdate,
  DocSummary,
  DocTranslateRequest,
  DocTranslationRevisionSummary,
  DocTranslationSummary,
  DocTranslationWrite,
  DocTreeResponse,
  DocWriteRequest,
} from './docs'
import type {
  DuplicateCheckResponse,
  DuplicateCheckResult,
  DuplicateCluster,
  DuplicateClusterEvent,
  NameLintIssue,
  NamingConvention,
} from './duplicates'
import type {
  Event,
  EventChange,
  EventFieldObservedValues,
  EventFieldValue,
  EventFieldVariableValue,
  EventIdentityHolder,
  EventIdentityHoldersResponse,
  EventListResponse,
  EventMetaValue,
  EventPhoto,
  EventPhotoComment,
  EventTag,
  ObservedFieldValue,
  PhotoLimits,
  SchemaDrift,
  SchemaDriftList,
} from './events'
import type {
  EventType,
  EventTypeBrief,
  FieldDefinition,
  MetaFieldUsage,
} from './eventTypes'
import type {
  ComponentAverage,
  EventHealth,
  EventHealthBrief,
  EventHealthListResponse,
  EventTypeHealth,
  EventTypeHealthListResponse,
  HealthComponent,
  ProjectHealthResponse,
  ProjectHealthTrendPoint,
} from './health'
import type {
  IncidentSummaryBody,
  IncidentSummaryFact,
  IncidentSummaryResponse,
  IncidentSummarySentence,
} from './incidentSummary'
import type {
  EventMigration,
  EventMigrationSide,
  LifecycleFinding,
} from './lifecycle'
import type {
  NotificationActor,
  NotificationPage,
  NotificationPrefs,
  SubscriptionState,
} from './notifications'
import type {
  ActivityItem,
  EventTypeOwner,
  Project,
  ProjectMember,
  ProjectSummary,
} from './projects'
import type {
  SearchResponse,
  SearchResult,
  SearchVariant,
  SearchVariantGroup,
} from './search'
import type {
  ImplementationTicket,
  ProjectTrackerConfig,
  ProjectTrackerConfigUpdate,
} from './tracker'
import type {
  ActiveStepIn,
  ApiKey,
  AuthUser,
  OrgMembership,
  UserListItem,
} from './users'
import type {
  AiAskResponse,
  AiAskSource,
  AiDescribeResponse,
  AiFieldSuggestion,
  AiStatus,
} from '../api/ai'
import type {
  AuthStatusResponse,
  PasswordResetConfirmResponse,
  PasswordResetRequestResponse,
  RegisterRequest,
} from '../api/auth'
import type {
  Invitation,
  InvitationCreated,
  InvitationPreview,
} from '../api/invitations'
import type {
  OrgUpdate,
} from '../api/orgs'
import type {
  AnomalyResetCounts,
  DetectionResetPeriod,
  DriftResetCounts,
  VariableRetirementCounts,
} from '../api/projects'
import type {
  PropertyDrift,
  PropertyDriftList,
} from '../api/propertyDrifts'
import type {
  CoverageBucket,
  CoverageResponse,
  CoverageSummary,
  ShadowEventBatchItem,
  ShadowEventBatchItemResult,
  ShadowEventBatchResponse,
} from '../api/reconciliation'
import type {
  VariableEventOverride,
} from '../api/variableOverrides'

/**
 * Compile-time drift checks for API types that are still written by hand
 * instead of aliased from the generated schema (`api.gen.ts`). Each entry
 * fails `tsc` the moment the backend adds or drops a field the hand-written
 * type does not (or the other way round), so a regenerated `api.gen.ts` cannot
 * drift away from them silently. Optionality and value types are not compared
 * here, only the field names, except where a check says otherwise: turning one
 * of these into `type X = components['schemas']['X']` is the full fix, and
 * its entry goes when it is.
 *
 * Nothing imports this module at runtime; it exists for `tsc -b`, which checks
 * every file under `src/`.
 */
type Schemas = components['schemas']

/** True when A and B are the same type (not merely mutually assignable). */
type Equal<A, B> =
  (<T>() => T extends A ? 1 : 2) extends (<T>() => T extends B ? 1 : 2) ? true : false
type SameKeys<Hand, Generated> = Equal<keyof Hand, keyof Generated>
/** Same fields with the same value types, whichever side marks one optional. */
type SameShapeIgnoringOptional<Hand, Generated> = [Required<Hand>] extends [Required<Generated>]
  ? [Required<Generated>] extends [Required<Hand>]
    ? true
    : false
  : false

type Expect<T extends true[]> = T

export type ApiTypeDriftChecks = Expect<
  [
    // types/scans.ts: kept by hand so its lists read as always present.
    SameKeys<ScanDryRunResponse, Schemas['ScanDryRunResponse']>,
    SameShapeIgnoringOptional<ScanDryRunResponse, Schemas['ScanDryRunResponse']>,
    // types/dataSourceSchema.ts
    SameKeys<ColumnSchema, Schemas['ColumnSchema']>,
    SameKeys<TableSchema, Schemas['TableSchema']>,
    SameKeys<DataSourceSchemaResponse, Schemas['DataSourceSchemaResponse']>,
    // types/dataSources.ts
    SameKeys<ConnectionSettingsResponse, Schemas['ConnectionSettingsResponse']>,
    SameKeys<DataSource, Schemas['DataSourceResponse']>,
    SameKeys<DataSourceScanRef, Schemas['DataSourceScanRef']>,
    // types/dependencies.ts
    SameKeys<DependencyEdge, Schemas['DependencyEdge']>,
    SameKeys<DependencyEntity, Schemas['DependencyEntity']>,
    SameKeys<DependenciesResponse, Schemas['DependenciesResponse']>,
    SameKeys<ImpactChange, Schemas['ImpactChange']>,
    SameKeys<ImpactItem, Schemas['ImpactItem']>,
    SameKeys<ImpactResponse, Schemas['ImpactResponse']>,
    // types/docs.ts
    SameKeys<DocSummary, Schemas['DocSummary']>,
    SameKeys<DocTreeResponse, Schemas['DocTreeResponse']>,
    SameKeys<DocLinkResolution, Schemas['DocLinkResolution']>,
    SameKeys<DocLinkSuggestion, Schemas['DocLinkSuggestion']>,
    SameKeys<DocLinkSuggestionsResponse, Schemas['DocLinkSuggestionsResponse']>,
    SameKeys<DocTranslationSummary, Schemas['DocTranslationSummary']>,
    SameKeys<DocLanguageDefaults, Schemas['DocLanguageDefaults']>,
    SameKeys<DocTranslateRequest, Schemas['DocTranslateRequest']>,
    SameKeys<DocTranslationWrite, Schemas['DocTranslationWrite']>,
    SameKeys<DocTranslationRevisionSummary, Schemas['DocTranslationRevisionSummary']>,
    SameKeys<DocWriteRequest, Schemas['DocWriteRequest']>,
    SameKeys<DocMoveRequest, Schemas['DocMoveRequest']>,
    SameKeys<DocMoveResponse, Schemas['DocMoveResponse']>,
    SameKeys<DocFolderDeleteResponse, Schemas['DocFolderDeleteResponse']>,
    SameKeys<DocRevisionSummary, Schemas['DocRevisionSummary']>,
    SameKeys<DocRevisionListResponse, Schemas['DocRevisionListResponse']>,
    SameKeys<DocSearchHit, Schemas['DocSearchHit']>,
    SameKeys<DocSearchResponse, Schemas['DocSearchResponse']>,
    SameKeys<DocBacklinkItem, Schemas['DocBacklinkItem']>,
    SameKeys<DocBacklinksResponse, Schemas['DocBacklinksResponse']>,
    SameKeys<DocBundleFile, Schemas['DocBundleFile']>,
    SameKeys<DocBundle, Schemas['DocBundle']>,
    SameKeys<DocImportRequest, Schemas['DocImportRequest']>,
    SameKeys<DocImportResult, Schemas['DocImportResult']>,
    SameKeys<DocSharingUpdate, Schemas['DocSharingUpdate']>,
    // types/duplicates.ts
    SameKeys<NameLintIssue, Schemas['NameLintIssue']>,
    SameKeys<NamingConvention, Schemas['NamingConventionOut']>,
    SameKeys<DuplicateCheckResult, Schemas['DuplicateCheckResult']>,
    SameKeys<DuplicateCheckResponse, Schemas['DuplicateCheckResponse']>,
    SameKeys<DuplicateClusterEvent, Schemas['DuplicateClusterEvent']>,
    SameKeys<DuplicateCluster, Schemas['DuplicateCluster']>,
    // types/events.ts
    SameKeys<EventFieldVariableValue, Schemas['EventFieldVariableValueResponse']>,
    SameKeys<EventFieldValue, Schemas['EventFieldValueResponse']>,
    SameKeys<ObservedFieldValue, Schemas['ObservedFieldValue']>,
    SameKeys<EventFieldObservedValues, Schemas['EventFieldObservedValues']>,
    SameKeys<EventMetaValue, Schemas['EventMetaValueResponse']>,
    SameKeys<EventTag, Schemas['EventTagResponse']>,
    SameKeys<Event, Schemas['EventResponse']>,
    SameKeys<EventChange, Schemas['EventChangeResponse']>,
    SameKeys<SchemaDrift, Schemas['SchemaDriftResponse']>,
    SameKeys<SchemaDriftList, Schemas['SchemaDriftListResponse']>,
    SameKeys<EventListResponse, Schemas['EventListResponse']>,
    SameKeys<EventIdentityHolder, Schemas['EventIdentityHolder']>,
    SameKeys<EventIdentityHoldersResponse, Schemas['EventIdentityHoldersResponse']>,
    SameKeys<EventPhoto, Schemas['EventPhotoResponse']>,
    SameKeys<PhotoLimits, Schemas['PhotoLimitsResponse']>,
    SameKeys<EventPhotoComment, Schemas['EventPhotoCommentResponse']>,
    // types/eventTypes.ts
    SameKeys<EventType, Schemas['EventTypeResponse']>,
    SameKeys<EventTypeBrief, Schemas['EventTypeBrief']>,
    SameKeys<FieldDefinition, Schemas['FieldDefinitionResponse']>,
    SameKeys<MetaFieldUsage, Schemas['MetaFieldUsageResponse']>,
    // types/health.ts
    SameKeys<HealthComponent, Schemas['HealthComponent']>,
    SameKeys<EventHealth, Schemas['EventHealth']>,
    SameKeys<EventHealthListResponse, Schemas['EventHealthListResponse']>,
    SameKeys<EventHealthBrief, Schemas['EventHealthBrief']>,
    SameKeys<ComponentAverage, Schemas['ComponentAverage']>,
    SameKeys<EventTypeHealth, Schemas['EventTypeHealth']>,
    SameKeys<EventTypeHealthListResponse, Schemas['EventTypeHealthListResponse']>,
    SameKeys<ProjectHealthTrendPoint, Schemas['ProjectHealthTrendPoint']>,
    SameKeys<ProjectHealthResponse, Schemas['ProjectHealthResponse']>,
    // types/incidentSummary.ts
    SameKeys<IncidentSummaryFact, Schemas['IncidentSummaryFact']>,
    SameKeys<IncidentSummarySentence, Schemas['IncidentSummarySentence']>,
    SameKeys<IncidentSummaryBody, Schemas['IncidentSummaryBody']>,
    SameKeys<IncidentSummaryResponse, Schemas['IncidentSummaryResponse']>,
    // types/lifecycle.ts
    SameKeys<LifecycleFinding, Schemas['LifecycleFindingResponse']>,
    SameKeys<EventMigrationSide, Schemas['EventMigrationSide']>,
    SameKeys<EventMigration, Schemas['EventMigrationResponse']>,
    // types/notifications.ts
    SameKeys<NotificationActor, Schemas['NotificationActor']>,
    SameKeys<NotificationPage, Schemas['NotificationPage']>,
    SameKeys<NotificationPrefs, Schemas['NotificationPrefsResponse']>,
    SameKeys<SubscriptionState, Schemas['SubscriptionState']>,
    // types/projects.ts
    SameKeys<EventTypeOwner, Schemas['EventTypeOwnerResponse']>,
    SameKeys<ProjectSummary, Schemas['ProjectSummary']>,
    SameKeys<Project, Schemas['ProjectResponse']>,
    SameKeys<ProjectMember, Schemas['ProjectMemberResponse']>,
    SameKeys<ActivityItem, Schemas['ActivityItemResponse']>,
    // types/search.ts
    SameKeys<SearchVariant, Schemas['SearchVariant']>,
    SameKeys<SearchVariantGroup, Schemas['SearchVariantGroup']>,
    SameKeys<SearchResult, Schemas['SearchResult']>,
    SameKeys<SearchResponse, Schemas['SearchResponse']>,
    // types/tracker.ts
    SameKeys<ProjectTrackerConfig, Schemas['ProjectTrackerConfigResponse']>,
    SameKeys<ProjectTrackerConfigUpdate, Schemas['ProjectTrackerConfigUpdate']>,
    SameKeys<ImplementationTicket, Schemas['ImplementationTicketResponse']>,
    // types/users.ts
    SameKeys<OrgMembership, Schemas['OrgMembershipOut']>,
    SameKeys<ActiveStepIn, Schemas['ActiveStepInOut']>,
    SameKeys<AuthUser, Schemas['AuthUserResponse']>,
    SameKeys<UserListItem, Schemas['UserListItem']>,
    SameKeys<ApiKey, Schemas['ApiKeyResponse']>,
    // api/ai.ts
    SameKeys<AiStatus, Schemas['AiStatusResponse']>,
    SameKeys<AiFieldSuggestion, Schemas['AiFieldSuggestion']>,
    SameKeys<AiDescribeResponse, Schemas['AiDescribeResponse']>,
    SameKeys<AiAskSource, Schemas['AiAskSource']>,
    SameKeys<AiAskResponse, Schemas['AiAskResponse']>,
    // api/auth.ts
    SameKeys<PasswordResetRequestResponse, Schemas['PasswordResetRequestResponse']>,
    SameKeys<PasswordResetConfirmResponse, Schemas['PasswordResetConfirmResponse']>,
    SameKeys<AuthStatusResponse, Schemas['AuthStatusResponse']>,
    SameKeys<RegisterRequest, Schemas['RegisterRequest']>,
    // api/invitations.ts
    SameKeys<Invitation, Schemas['InvitationResponse']>,
    SameKeys<InvitationCreated, Schemas['InvitationCreatedResponse']>,
    SameKeys<InvitationPreview, Schemas['InvitationPreview']>,
    // api/orgs.ts
    SameKeys<OrgUpdate, Schemas['OrgUpdate']>,
    // api/projects.ts
    SameKeys<DetectionResetPeriod, Schemas['DetectionResetPeriod']>,
    SameKeys<AnomalyResetCounts, Schemas['AnomalyResetCounts']>,
    SameKeys<VariableRetirementCounts, Schemas['VariableRetirementCounts']>,
    SameKeys<DriftResetCounts, Schemas['DriftResetCounts']>,
    // api/propertyDrifts.ts
    SameKeys<PropertyDrift, Schemas['PropertyDriftResponse']>,
    SameKeys<PropertyDriftList, Schemas['PropertyDriftListResponse']>,
    // api/reconciliation.ts
    SameKeys<ShadowEventBatchItem, Schemas['ShadowEventBatchItem']>,
    SameKeys<ShadowEventBatchItemResult, Schemas['ShadowEventBatchItemResult']>,
    SameKeys<ShadowEventBatchResponse, Schemas['ShadowEventBatchResponse']>,
    SameKeys<CoverageBucket, Schemas['CoverageBucket']>,
    SameKeys<CoverageSummary, Schemas['CoverageSummary']>,
    SameKeys<CoverageResponse, Schemas['CoverageResponse']>,
    // api/variableOverrides.ts
    SameKeys<VariableEventOverride, Schemas['VariableEventOverrideResponse']>,
  ]
>
