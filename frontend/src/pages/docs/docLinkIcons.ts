import {
  AtSign,
  BellRing,
  Database,
  FileText,
  Gauge,
  GitBranch,
  Hash,
  ScanSearch,
  Shapes,
  Variable,
  Zap,
  type LucideIcon,
} from 'lucide-react'
import type { DocLinkKind } from '@/types/docs'

/** One icon per link kind: the link picker rows and the rendered links. */
export const DOC_LINK_KIND_ICON: Record<DocLinkKind, LucideIcon> = {
  event: Zap,
  event_type: Shapes,
  field: Hash,
  doc: FileText,
  variable: Variable,
  metric: Gauge,
  alert_rule: BellRing,
  branch: GitBranch,
  scan: ScanSearch,
  data_source: Database,
  user: AtSign,
}

/**
 * Kinds whose rendered link carries its icon: the F24 kinds. The F22 plan
 * links (event, event type, field) stay plain monospace, and a mention is
 * already marked by its `@`.
 */
export const INLINE_ICON_KINDS: ReadonlySet<DocLinkKind> = new Set<DocLinkKind>([
  'doc',
  'variable',
  'metric',
  'alert_rule',
  'branch',
  'scan',
  'data_source',
])
