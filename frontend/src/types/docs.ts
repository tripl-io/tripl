/**
 * Docs catalog (F22, GH #299): Markdown notes for people and AI agents.
 *
 * HAND-WRITTEN mirror of `backend/src/tripl/schemas/docs.py`. Keep the two in
 * step: a field added there and missing here compiles fine and is simply never
 * read. Timestamps are ISO strings, UUIDs are strings.
 */

/** Which root a note lives under: the project's own, or its organization's. */
export type DocScope = 'project' | 'organization'
export const DOC_SCOPES: readonly DocScope[] = ['project', 'organization']

/** Who a note is written for. Missing frontmatter reads as `both`. */
export type DocAudience = 'human' | 'agent' | 'both'

export type DocLinkKind = 'event' | 'event_type' | 'field'
export type DocLinkStatus = 'resolved' | 'ambiguous' | 'broken'
export type DocRevisionAction = 'create' | 'update' | 'move' | 'restore' | 'import'
export type DocImportMode = 'merge' | 'mirror'

/**
 * Who may read a note (F24, GH #308). `level` is everyone with access to the
 * note's project or organization (a new note's default); `restricted` is the
 * author plus the people and groups it is shared with; `private` is the author
 * only. Sharing never grants project access: a share to someone outside the
 * project or organization is ignored.
 */
export type DocVisibility = 'private' | 'restricted' | 'level'
export const DOC_VISIBILITIES: readonly DocVisibility[] = ['private', 'restricted', 'level']
/** What the caller may do with a note, or what a share grants. */
export type DocPermission = 'view' | 'edit'
export type DocSharePrincipalType = 'user' | 'group'

export interface DocSummary {
  scope: DocScope
  path: string
  title: string
  description: string
  tags: string[]
  audience: DocAudience
  revision: number
  size_bytes: number
  updated_at: string
  updated_by_name: string | null
  /** The note's effective visibility (its own, or the nearest folder's). */
  visibility: DocVisibility
  /** What the caller may do with it; a note it cannot read is never listed. */
  my_permission: DocPermission
  /** True when the note is shared with at least one person or group. */
  shared: boolean
}

export interface DocTreeLimits {
  max_file_bytes: number
  max_files_per_scope: number
  max_bundle_files: number
  max_bundle_bytes: number
}

export interface DocTreeResponse {
  project: { slug: string; name: string }
  organization: { id: string; slug: string; name: string }
  project_docs: DocSummary[]
  organization_docs: DocSummary[]
  limits: DocTreeLimits
}

export interface DocLinkRef {
  kind: DocLinkKind
  target: string
  qualifier: string | null
  label: string | null
  raw: string
}

export interface DocLinkResolution {
  kind: DocLinkKind
  target: string
  qualifier: string | null
  raw: string
  status: DocLinkStatus
  route_path: string | null
  entity_id: string | null
  candidates: number
}

export interface DocFileResponse extends DocSummary {
  id: string
  /** Raw content, frontmatter included — what the editor edits. */
  content: string
  /** Content with the frontmatter block stripped — what the page renders. */
  body: string
  /** Frontmatter keys tripl does not interpret, kept verbatim. */
  extra_frontmatter: Record<string, unknown>
  links: DocLinkResolution[]
  created_at: string
  created_by_name: string | null
  /**
   * True when an organization owner or admin opened a note hidden from them:
   * an audited break-glass read (`doc.break_glass_read`) that never grants editing.
   */
  break_glass?: boolean
}

export interface DocWriteRequest {
  content: string
  base_revision?: number | null
  create_only?: boolean
  message?: string
}

export interface DocWriteResponse extends DocFileResponse {
  created: boolean
  changed: boolean
  warnings: string[]
}

export interface DocMoveRequest {
  scope: DocScope
  from_path: string
  to_path: string
  folder?: boolean
}

export interface DocMoveResponse {
  moved: { from_path: string; to_path: string }[]
}

export interface DocFolderDeleteResponse {
  deleted: string[]
}

export interface DocRevisionSummary {
  id: string
  number: number
  action: DocRevisionAction
  path: string
  message: string
  author_name: string | null
  created_at: string
  content_sha256: string
  size_bytes: number
  restored_from_number: number | null
}

export interface DocRevisionListResponse {
  scope: DocScope
  path: string
  current_revision: number
  items: DocRevisionSummary[]
}

export interface DocRevisionDetail extends DocRevisionSummary {
  content: string
  /** Unified diff against the previous revision; "" for the first. */
  diff: string
  diff_truncated: boolean
}

export interface DocSearchHit {
  scope: DocScope
  path: string
  title: string
  description: string
  tags: string[]
  audience: DocAudience
  snippet: string
  score: number
  confidence: number
}

export interface DocSearchResponse {
  items: DocSearchHit[]
  total: number
  truncated: boolean
  semantic_used: boolean
}

export interface DocBacklinkItem {
  scope: DocScope
  path: string
  title: string
  description: string
  audience: DocAudience
  link_raw: string
}

export interface DocBacklinksResponse {
  kind: DocLinkKind
  name: string
  qualifier: string | null
  items: DocBacklinkItem[]
}

export interface DocBundleFile {
  path: string
  content: string
  sha256: string | null
}

export interface DocBundle {
  format: 'tripl-docs/v1'
  scope: DocScope
  project_slug: string
  organization_slug: string | null
  exported_at: string
  files: DocBundleFile[]
}

export interface DocImportRequest {
  format: 'tripl-docs/v1'
  files: DocBundleFile[]
}

export interface DocImportResult {
  scope: DocScope
  mode: DocImportMode
  dry_run: boolean
  created: string[]
  updated: string[]
  unchanged: string[]
  deleted: string[]
  skipped: { path: string; reason: string }[]
  errors: { path: string; detail: string }[]
}

/** One share of a note or folder: a person or an organization group. */
export interface DocShare {
  principal_type: DocSharePrincipalType
  principal_id: string
  /** The user's name (or email) or the group's name; resolved by the server. */
  name: string
  permission: DocPermission
}

/**
 * `GET .../docs/file/sharing` and `.../docs/folder/sharing`. `inherited` is
 * true when the note (or folder) follows the nearest folder setting above it,
 * named by `inherited_from` (a folder prefix; null when the default applies).
 */
export interface DocSharing {
  visibility: DocVisibility
  inherited: boolean
  inherited_from: string | null
  shares: DocShare[]
  /**
   * Whether the caller may change this setting. Optional: when the server
   * omits it the page's own guess (author or organization admin) stands.
   */
  can_manage?: boolean
}

/** `PUT` body: the shares without their display names. */
export interface DocSharingUpdate {
  visibility: DocVisibility
  inherited: boolean
  shares: Omit<DocShare, 'name'>[]
}
