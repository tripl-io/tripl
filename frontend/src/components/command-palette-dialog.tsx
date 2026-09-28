import {
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import { keepPreviousData, useMutation, useQuery } from '@tanstack/react-query'
import { Command } from 'cmdk'
import {
  Activity,
  Bell,
  BookOpen,
  FileText,
  Folder,
  Gauge,
  GitBranch,
  Layers,
  LayoutDashboard,
  Link2,
  List,
  ListChecks,
  Loader2,
  LogOut,
  Plus,
  Search,
  Settings,
  SlidersHorizontal,
  Sparkles,
  SunMoon,
  Table2,
  Tag,
  UserPlus,
  Variable,
} from 'lucide-react'
import { toast } from 'sonner'
import { aiApi } from '@/api/ai'
import { eventTypesApi } from '@/api/eventTypes'
import { planBranchesApi } from '@/api/planBranches'
import { searchApi } from '@/api/search'
import { ActiveProjectContext } from '@/components/active-project-context'
import { useAuth } from '@/components/auth-context'
import { useCommandPalette } from '@/components/command-palette-context'
import { PALETTE_ITEM_CLASS } from '@/components/palette-item'
import { SearchVariantCount, SearchVariantRows } from '@/components/search-variants'
import { PROJECT_GROUPS, WORKSPACE_GROUPS } from '@/components/settings/nav'
import { useTheme } from '@/components/theme-provider'
import { eventNameLabel } from '@/lib/eventName'
import { buildNavGroups, currentOrgSlug, projectHomePath, projectPath, settingsPath, switchProjectPath, withActiveOrg, workspacePath } from '@/lib/navigation'
import { isOnboardingDismissed, setOnboardingDismissed } from '@/lib/onboardingDismissal'
import { useBranchContext, useBranchLinkProps } from '@/hooks/useBranch'
import { requestPageLeave } from '@/hooks/useUnsavedChangesGuard'
import { useAiStatus } from '@/hooks/useAiStatus'
import { SEARCH_DEBOUNCE_MS, useDebouncedValue } from '@/hooks/useDebouncedValue'
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog'
import { Chip } from '@/components/primitives/chip'
import { Kbd } from '@/components/primitives/kbd'
import type { AiAskResponse } from '@/api/ai'
import type { SearchEntityType, SearchResult } from '@/types'
import {
  commandPaletteLexicalSearchKey,
  commandPaletteSearchKey,
  eventTypesKey,
  planBranchesKey,
  projectsQueryOptions,
} from '@/lib/queryKeys'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { canWrite, isOwner as isOwnerRole } from '@/lib/permissions'


/**
 * The command palette dialog itself: cmdk, the knowledge search, the AI ask
 * and every static destination. Its own chunk, loaded on the first Ctrl+K or
 * trigger hover — the shell keeps only the shortcut and the open state
 * (command-palette.tsx), so no page load pays for a dialog most sessions
 * never open (#194 SHELL-4).
 */

const SEARCH_TYPE_META: Record<
  SearchEntityType,
  {
    heading: string
    icon: React.ComponentType<{ className?: string; style?: React.CSSProperties }>
  }
> = {
  event: { heading: 'Events', icon: Tag },
  event_type: { heading: 'Event types', icon: Layers },
  field: { heading: 'Fields', icon: List },
  meta_field: { heading: 'Meta fields', icon: FileText },
  variable: { heading: 'Variables', icon: Variable },
  relation: { heading: 'Relations', icon: Link2 },
  tag: { heading: 'Tags', icon: Tag },
  metric: { heading: 'Metrics', icon: Gauge },
  fact_table: { heading: 'Fact tables', icon: Table2 },
  scan_config: { heading: 'Scans', icon: Activity },
  alert_rule: { heading: 'Alert rules', icon: Bell },
  doc: { heading: 'Docs', icon: FileText },
}

/**
 * Buckets the ranked result list by entity type, preserving the server's order.
 *
 * A `Map` keyed on first appearance is doing load-bearing work here, not just
 * grouping: the groups come out in the order their best result arrived, and each
 * group's rows stay in the order the API sent them. That is the ordering the
 * palette now renders verbatim (tripl-k6gt), so a `sort` added anywhere in this
 * function would undo the fix without touching the component.
 *
 * It does NOT fully preserve rank ACROSS groups: for [event 0.99, variable 0.98,
 * event 0.10] the weak event still lands above the strong variable, because its
 * group opened first. That is a known limitation of showing typed headings at
 * all, and it is a separate decision from the client re-scoring this commit
 * removes.
 */
function groupSearchResults(results: SearchResult[]) {
  const groups = new Map<SearchEntityType, SearchResult[]>()
  for (const result of results) {
    const items = groups.get(result.entity_type) ?? []
    items.push(result)
    groups.set(result.entity_type, items)
  }
  return Array.from(groups.entries())
}

type PaletteIcon = React.ComponentType<{
  className?: string
  style?: React.CSSProperties
}>

declare const paletteValueBrand: unique symbol
/**
 * cmdk's identity for one row — namespaced, and branded so it cannot be typed by
 * hand at a call site.
 *
 * `Item`'s `value` used to be `${label} ${hint} ${keywords}`: a string built to
 * be FUZZY-SCORED, back when cmdk's own filter ran. With that filter off
 * (tripl-k6gt) the string has exactly one job left — cmdk marks a row selected by
 * comparing its `value` against the list's current value — and two rows carrying
 * one string are BOTH announced as `aria-selected`. Two rows do collide once the
 * keyword stuffing is gone: the static Event types row is label=display_name,
 * hint=name, and the server's own event_type document is title=display_name,
 * subtitle=name (backend/src/tripl/services/_search_documents.py:339-343) — the
 * same two strings in the same order. Searching for an event type you can also
 * see in the static list is not an exotic case, it is the common one.
 *
 * The brand is why this is a compile error rather than a comment: every value has
 * to come from `paletteValue`, so a new row cannot be added without picking a
 * namespace, and no namespace can be reached from another one's inputs.
 */
type PaletteValue = string & { readonly [paletteValueBrand]: true }

const paletteValue = {
  nav: (path: string) => `nav:${path}` as PaletteValue,
  project: (projectId: string) => `project:${projectId}` as PaletteValue,
  eventType: (eventTypeId: string) => `event-type:${eventTypeId}` as PaletteValue,
  search: (documentId: string) => `search:${documentId}` as PaletteValue,
  variants: (groupKey: string) => `variants:${groupKey}` as PaletteValue,
  account: (action: string) => `account:${action}` as PaletteValue,
  action: (action: string) => `action:${action}` as PaletteValue,
  branch: (branchId: string) => `branch:${branchId}` as PaletteValue,
  ai: () => 'ai:ask' as PaletteValue,
}

/** One static row, as data, so its group can decide whether it survives typing. */
interface PaletteRow {
  value: PaletteValue
  label: string
  hint?: string
  /**
   * Other words a row answers to, matched by word prefix (not shown). The old
   * name of a renamed page ("Schema & fields"), or what a settings section
   * holds ("timezone" for General) (#238 JR-19).
   */
  keywords?: string[]
  icon: PaletteIcon
  iconColor?: string
  active?: boolean
  onSelect: () => void
}

/** A heading and the rows under it, before or after narrowing. */
interface PaletteGroup {
  heading: string
  rows: PaletteRow[]
}

/**
 * Does a static row survive what has been typed so far?
 *
 * cmdk used to answer this, and taking the job off it is the whole of tripl-k6gt:
 * its `shouldFilter` prop covers filtering and SORTING together, so leaving it on
 * to keep the static groups responsive also let it re-append every knowledge
 * result in its own fuzzy-score order (cmdk 1.1.1 `dist/index.mjs` sorts by
 * commandScore, then `appendChild`s each node) — throwing away the relevance
 * ranking the backend spent #113, #114 and a migration computing.
 *
 * Substring rather than a fuzzy score, deliberately. Nothing static is RANKED: a
 * group shows a row or it does not, and the rows inside it stay in the order they
 * are written in. A score here would reintroduce the reordering in miniature.
 *
 * An empty query matches everything, which is what makes a freshly opened palette
 * show the full menu.
 */
function matchesQuery(
  query: string,
  haystack: (string | undefined | null)[],
  keywords: readonly string[] = [],
): boolean {
  const needle = query.trim().toLowerCase()
  if (!needle) return true
  // Every word must land somewhere, in any order: "fact table" missed while
  // "revenue" found the Orders fact table, because the phrase was matched
  // whole (#238 JR-19). Keywords match by word prefix only, so a one-letter
  // query does not light up every row with that letter inside a keyword.
  const keywordWords = keywords.flatMap(keyword => keyword.toLowerCase().split(/\s+/))
  return needle.split(/\s+/).every(
    token =>
      haystack.some(term => term?.toLowerCase().includes(token))
      || keywordWords.some(word => word.startsWith(token)),
  )
}

/**
 * Narrows each group against the query and drops the ones left with no rows.
 *
 * The narrowing has to happen HERE, in the caller, rather than inside a group
 * component that hides itself: the list-wide "No matches." line has to know
 * whether any static row survived at all, and a component that decides its own
 * visibility can only report that to the DOM. Deriving the survivors up front is
 * what lets the empty state be computed once instead of being guessed twice —
 * "No matches." and "No knowledge matches." used to render on the same
 * keystroke because each was answering a different question.
 *
 * Dropping the whole group and not just its rows is load-bearing on cmdk 1.1.1:
 * with `shouldFilter={false}` a `Command.Group`'s `hidden` computation
 * short-circuits to visible, so a group whose every child returned `null` would
 * keep its heading standing over nothing.
 */
function visibleStaticGroups(query: string, groups: PaletteGroup[]): PaletteGroup[] {
  return groups
    .map(group => ({
      heading: group.heading,
      rows: group.rows.filter(row => matchesQuery(query, [row.label, row.hint], row.keywords)),
    }))
    .filter(group => group.rows.length > 0)
}

/**
 * What the knowledge section of the list is showing right now — exactly one of
 * these, ever.
 *
 * `error` exists because it used to be indistinguishable from `empty`: a failed
 * request left `searchQuery.data?.items ?? []` at zero length and the palette
 * announced "No knowledge matches.", i.e. told the user their knowledge base
 * held nothing when in fact the request never completed.
 *
 * `off` is the only state in which the list-wide empty line may render, which is
 * what makes the two empty states mutually exclusive by construction.
 */
type KnowledgeState = 'off' | 'searching' | 'error' | 'empty' | 'results'

/**
 * How far the previous query's rows are dimmed while the next search runs. Far
 * enough to read as "not the answer yet", not so far that they stop being
 * legible — they are still the rows Enter will act on (tripl-2x5d).
 */
const STALE_RESULT_OPACITY = 0.55

/**
 * Words people type for a project page that its label does not contain: the
 * names pages had before a rename, and the object nouns other screens use.
 */
const NAV_KEYWORDS: Record<string, string[]> = {
  overview: ['live activity', 'home', 'dashboard', 'kpi'],
  schema: ['schema & fields', 'schema', 'custom properties'],
  alerting: ['monitors', 'alert rules', 'incidents', 'inbox', 'destinations'],
  anomalies: ['signals', 'spikes', 'drops'],
  metrics: ['fact tables', 'kpi'],
  scans: ['data source', 'sql'],
  branches: ['review', 'merge'],
  history: ['snapshots', 'revisions'],
  duplicates: ['merge', 'near-duplicates', 'naming', 'successor'],
  docs: ['notes', 'wiki', 'markdown', 'catalog', 'skills', 'agents'],
}

/**
 * Settings rows whose rail label collides with a project page in the same
 * list: the project's Govern › Audit log is filtered to the project, the
 * instance one is not.
 */
const SETTINGS_LABEL: Record<string, string> = {
  'inst-audit': 'Instance audit log',
}

/** What each settings section holds, keyed by settings/nav.ts item id. */
const SETTINGS_KEYWORDS: Record<string, string[]> = {
  general: ['timezone', 'slug', 'rename', 'project name'],
  'plan-rules': ['naming', 'rules'],
  members: ['users', 'roles', 'invite', 'team'],
  sources: ['database', 'connection', 'warehouse', 'clickhouse', 'postgres'],
  apikeys: ['api key', 'token', 'tracker'],
  profile: ['name', 'email', 'avatar'],
  security: ['password', 'sessions', 'sign in'],
  runtime: ['workers', 'jobs', 'celery'],
  email: ['smtp', 'mail'],
  ai: ['openai', 'llm', 'embeddings', 'model'],
  'inst-security': ['registration', 'sign up', 'access'],
  storage: ['retention', 'files', 'photos'],
  observability: ['logs', 'metrics', 'tracing'],
  system: ['version', 'health'],
  'inst-audit': ['audit log', 'history'],
}

/**
 * Is `next` the search that produced `held`, one keystroke on?
 *
 * Either direction, because both typing and backspacing refine: "check" holds
 * while "checko" is in flight, and "checkout" holds while "checkou" is. An empty
 * `held` means this palette session has never had rows, so there is nothing it
 * is entitled to keep.
 */
function isSearchRefinement(held: string, next: string): boolean {
  if (!held) return false
  return held.startsWith(next) || next.startsWith(held)
}

export default function CommandPalette({
  onRestoreFocus,
  onNavigate,
}: {
  onRestoreFocus: () => void
  /** Called when a command closes the palette by navigating. */
  onNavigate: () => void
}) {
  const { open, setOpen } = useCommandPalette()
  const { resolvedTheme, setTheme } = useTheme()
  const navigate = useNavigate()
  const location = useLocation()
  const auth = useAuth()
  const { slug: routeSlug } = useParams()
  const { branchId, setBranchId } = useBranchContext()
  const branchLink = useBranchLinkProps()
  const [query, setQuery] = useState('')
  const debouncedQuery = useDebouncedValue(query.trim(), SEARCH_DEBOUNCE_MS)
  const [aiQuestion, setAiQuestion] = useState<string | null>(null)
  const [aiResult, setAiResult] = useState<AiAskResponse | null>(null)
  const aiBackRef = useRef<HTMLButtonElement | null>(null)

  // Every close starts the next open fresh. Radix reports only Esc and outside
  // clicks through `onOpenChange`; running a command, following an AI source or
  // pressing Ctrl+K again closes through the context directly, and the palette
  // itself stays mounted — so the next Ctrl+K reopened on the previous AI
  // answer, with no search input (SHELL-26). Adjusted during render, like the
  // held search rows below, so no frame shows the old state.
  const [wasOpen, setWasOpen] = useState(open)
  if (wasOpen !== open) {
    setWasOpen(open)
    if (!open) {
      setQuery('')
      setAiQuestion(null)
      setAiResult(null)
    }
  }


  const projectsQuery = useQuery({ ...projectsQueryOptions(), enabled: open })
  const projects = projectsQuery.data ?? []
  // The project the shell resolved for this address first: the list may not
  // have landed yet, or may not show a project a deep link opened.
  const shellProject = useContext(ActiveProjectContext)
  const activeProject =
    (shellProject && shellProject.slug === routeSlug ? shellProject : null)
    ?? projects.find(p => p.slug === routeSlug)
    ?? null

  const eventTypesQuery = useQuery({
    queryKey: eventTypesKey(activeProject?.slug, branchId),
    queryFn: () => eventTypesApi.list(activeProject!.slug, branchId),
    enabled: open && !!activeProject,
    staleTime: 60_000,
  })
  const eventTypes = eventTypesQuery.data ?? []

  // The branch list the sidebar's switcher already loads for this project.
  // Read from the cache only (no request of the palette's own): the switcher
  // keeps it fresh, and without it the palette falls back to the branches page.
  const branchesQuery = useQuery({
    queryKey: planBranchesKey(activeProject?.slug),
    queryFn: () => planBranchesApi.list(activeProject!.slug),
    enabled: false,
  })

  // Only the project in the address. Outside one (/workspace, a 404) this used
  // to fall back to projects[0], so knowledge results and AI answers came from
  // whichever project sorted first, under a heading that named none (SHELL-27).
  const searchSlug = activeProject?.slug ?? null
  const searchEnabled = open && !!searchSlug && debouncedQuery.length >= 2
  // Two answers per query, cheapest first (tripl-kjhi.15). On production the
  // full search took 0.5–1.8 s and every millisecond past the lexical SQL was
  // the embedding round trip; a palette is typed into, and a list that lands
  // in ~150 ms is one the reader keeps typing against. So the keyword-only
  // answer is asked for alongside the full one and shown until the full one
  // — the same rows re-ranked, plus the semantic matches — replaces it.
  const lexicalQuery = useQuery({
    queryKey: commandPaletteLexicalSearchKey(searchSlug, debouncedQuery),
    // Every debounce boundary supersedes the previous key; the signal cancels
    // the superseded request instead of letting it queue on the backend.
    queryFn: ({ signal }) =>
      searchApi.search(
        searchSlug!,
        { q: debouncedQuery, limit: 12, semantic: false, group_variants: true },
        null,
        signal,
      ),
    enabled: searchEnabled,
    staleTime: 30_000,
  })
  const searchQuery = useQuery({
    meta: SILENT_ERROR_META,
    queryKey: commandPaletteSearchKey(searchSlug, debouncedQuery),
    queryFn: ({ signal }) =>
      searchApi.search(
        searchSlug!,
        { q: debouncedQuery, limit: 12, group_variants: true },
        null,
        signal,
      ),
    enabled: searchEnabled,
    staleTime: 30_000,
    // The key carries the DEBOUNCED text, so every 200ms boundary mints a new
    // key whose `data` starts undefined. Without a placeholder the rows already
    // on screen were thrown away mid-word and the list fell back to a single
    // "Searching." line for the length of the round trip — measured at >2.2s,
    // which is long enough that a reader concludes nothing matched and hits
    // Escape (tripl-2x5d). Same treatment as the audit table's paging query.
    placeholderData: keepPreviousData,
  })
  // Which query the rows on screen answer, or '' when this session has none.
  // React Query's observer keeps its last defined data for its own lifetime, and
  // that is the PAGE's lifetime here: CommandPaletteProvider mounts the palette
  // permanently and only the Dialog unmounts. So `placeholderData` above hands
  // the same rows back to every later query, including a session that has typed
  // something unrelated — the previous search's results, dimmed under "Updating
  // results…" and still the rows Enter acts on, above an input reading nothing
  // like them.
  const [heldQuery, setHeldQuery] = useState('')
  // Dropping below the two-character floor abandons the search, whichever way it
  // happened — clearing the input, Esc, or running a command. Adjusted during
  // render rather than in an effect: an effect would leave one painted frame in
  // which stale rows are still presented as answering the new input.
  //
  // "Settled" means the rows answer the DEBOUNCED text: the full answer once it
  // is no longer a placeholder, or the lexical one (which carries no
  // placeholder, so success alone means this key).
  const fullSettled = searchQuery.isSuccess && !searchQuery.isPlaceholderData
  const lexicalSettled = lexicalQuery.isSuccess
  const settledQuery =
    query.trim().length < 2 ? '' : fullSettled || lexicalSettled ? debouncedQuery : heldQuery
  if (settledQuery !== heldQuery) setHeldQuery(settledQuery)

  const searchResults = useMemo(() => {
    if (fullSettled) return searchQuery.data?.items ?? []
    if (lexicalSettled) return lexicalQuery.data?.items ?? []
    return !searchQuery.isPlaceholderData || isSearchRefinement(heldQuery, debouncedQuery)
      ? searchQuery.data?.items ?? []
      : []
  }, [
    fullSettled,
    lexicalSettled,
    searchQuery.data,
    lexicalQuery.data,
    searchQuery.isPlaceholderData,
    heldQuery,
    debouncedQuery,
  ])
  const searchGroups = useMemo(() => groupSearchResults(searchResults), [searchResults])

  const aiEnabled = useAiStatus(searchSlug)

  const askMutation = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (question: string) =>
      aiApi.ask(searchSlug!, question, branchId),
    onSuccess: data => setAiResult(data),
  })

  // `mutate` is stable for the observer's lifetime, unlike the mutation
  // result object, and the mutationFn reads slug/branch at call time.
  const askAi = askMutation.mutate
  const handleAskAi = useCallback(
    (question: string) => {
      setAiQuestion(question)
      setAiResult(null)
      askAi(question)
    },
    [askAi],
  )

  const handleBackFromAi = useCallback(() => {
    setAiQuestion(null)
    setAiResult(null)
    askMutation.reset()
  }, [askMutation])

  // The search input unmounts in AI mode, which left focus on the dialog body
  // (SHELL-28). Hand it to the way back; leaving AI mode remounts the input,
  // which focuses itself.
  useEffect(() => {
    if (aiQuestion) aiBackRef.current?.focus()
  }, [aiQuestion])

  const runCommand = useCallback(
    (action: () => void) => {
      setQuery('')
      setOpen(false)
      action()
    },
    [setOpen],
  )

  // Tells the provider the close is a navigation, so the focus restore below
  // lands on the new page's content rather than the opener (SHELL-25).
  const goTo = useCallback(
    (path: string) =>
      runCommand(() => {
        onNavigate()
        navigate(path)
      }),
    [navigate, onNavigate, runCommand],
  )

  // Knowledge results and AI sources arrive with a bare `route_path`, but the
  // rows they name were searched on the ACTIVE branch. Landing on the plain
  // path leaves the branch out of the URL, so the address the reader then
  // copies opens a 404 in a fresh session — the event only exists on that
  // branch (tripl-kjhi.7). Only `to` is used: the palette never leaves the
  // branch it searched in, so there is nothing for the click half to set.
  const goToResult = useCallback(
    (routePath: string) => goTo(branchLink(withActiveOrg(routePath), branchId).to),
    [branchId, branchLink, goTo],
  )

  const showAskAiAction = aiEnabled && searchSlug && debouncedQuery.length >= 8

  // A route row's path is its hint, its identity and its action all at once, so
  // it is written once. Every nav row was previously three lines of JSX repeating
  // the same string three times; the array form is what lets `StaticGroup` filter
  // them at all, since a row has to be data before a group can count how many of
  // them are left (tripl-k6gt).
  //
  // No path on screen (#238 SH-19 / JR-20): every row printed its raw route in
  // mono, which was noise, truncated labels at 390 and showed that Plan pages
  // sit under /settings/. The group heading says where a row lives.
  const navRow = (
    path: string,
    label: string,
    icon: PaletteIcon,
    keywords?: string[],
  ): PaletteRow => ({
    value: paletteValue.nav(path),
    label,
    keywords,
    icon,
    onSelect: () => goTo(path),
  })

  const isOwner = isOwnerRole(auth.user?.role)
  const canEdit = canWrite(auth.user?.role)

  // Workspace destinations: the portfolio, then every settings section the
  // role can open, built from the settings rail's own model so the palette
  // cannot drift from it. It used to list five rows, so "api key", "timezone"
  // or "email" found nothing (#238 JR-19). Labels are the rail's (each with
  // its own icon, not one sliders icon for all); the words people type for
  // what a section holds are keywords.
  const settingsRows: PaletteRow[] = [...WORKSPACE_GROUPS, ...PROJECT_GROUPS].flatMap(group =>
    group.items
      .filter(item => !item.ownerOnly || isOwner)
      // Project sections are bound to a project by the address (SHELL-20);
      // with none open there is nothing for them to configure.
      .filter(item => !item.path.startsWith('project/') || !!activeProject)
      .map(item => {
        const path = item.path.startsWith('project/') && activeProject
          ? settingsPath(`/settings/${item.path}?project=${encodeURIComponent(activeProject.slug)}`)
          : settingsPath(`/settings/${item.path}`)
        const label = group.label === 'Project'
          ? `Project settings: ${item.label}`
          : (SETTINGS_LABEL[item.id] ?? item.label)
        // The rail's own keywords too, so the palette finds what the settings
        // palette finds ("dark mode", "delete project"), plus the extra words
        // kept here (JR-19).
        const keywords = [...(item.keywords ?? []), ...(SETTINGS_KEYWORDS[item.id] ?? [])]
        return navRow(path, label, item.icon, keywords.length > 0 ? keywords : undefined)
      }),
  )
  const navigateRows: PaletteRow[] = [
    navRow(workspacePath(), 'All projects', LayoutDashboard, ['portfolio', 'workspace']),
    ...settingsRows,
  ]

  // Built from the sidebar's own nav model rather than restated here. The
  // hand-written subset this replaces had drifted: 8 destinations (Live
  // activity, Metrics, Anomalies, Plan branches, Reconciliation, Coverage, Audit
  // log, Concepts) had no row at all, so typing "anomalies" matched nothing and
  // fell through to knowledge search, and three surviving rows carried names the
  // sidebar had retired (tripl-m6cv). `ownerOnly` is filtered exactly as
  // app-sidebar.tsx does — offering a non-owner the Audit log row would walk
  // them into the 403 the sidebar is careful not to show them.
  const projectNavGroups: PaletteGroup[] = activeProject
    ? buildNavGroups(activeProject.slug, activeProject.summary).map(group => ({
        heading: `${group.label} — ${activeProject.name}`,
        rows: group.items
          .filter(item => !item.ownerOnly || isOwner)
          .map(item => navRow(item.href, item.label, item.icon, NAV_KEYWORDS[item.id])),
      }))
    : []

  // The project destinations that exist outside the nav model: two the sidebar
  // renders inline (Project settings below the groups, Concepts in its footer)
  // and one that is nobody's sidebar entry — the detection settings, which the
  // Anomalies item claims by match. Named as the page names itself; "Monitoring
  // settings" was a third name for it.
  const projectExtraRows: PaletteRow[] = activeProject
    ? [
        navRow(projectPath(currentOrgSlug(), activeProject.slug, '/settings'), 'Project settings', Settings),
        navRow(projectPath(currentOrgSlug(), activeProject.slug, '/concepts'), 'Concepts', BookOpen, ['glossary', 'help']),
        navRow(
          projectPath(currentOrgSlug(), activeProject.slug, '/settings/monitoring'),
          'Detection settings',
          SlidersHorizontal,
          ['sensitivity', 'threshold', 'monitoring'],
        ),
        // The way back to a dismissed getting-started checklist (WS-35).
        ...(isOnboardingDismissed(activeProject.slug, activeProject.id)
          ? [
              {
                value: paletteValue.nav('onboarding'),
                label: 'Show getting started',
                hint: 'Bring back the setup checklist',
                icon: ListChecks,
                onSelect: () => {
                  setOnboardingDismissed(activeProject.slug, activeProject.id, false)
                  goTo(projectHomePath(activeProject.slug))
                },
              },
            ]
          : []),
      ]
    : []

  // The slug is the hint, so it is already in the haystack `StaticGroup` matches
  // against — which is what keeps a project findable by slug now that cmdk is no
  // longer scoring a stuffed keyword string.
  const projectRows: PaletteRow[] = projects.map(project => ({
    value: paletteValue.project(project.id),
    label: project.name,
    hint: project.slug,
    icon: Folder,
    active: project.slug === routeSlug,
    // The same landing rule as the sidebar's switcher (SHELL-44).
    onSelect: () => goTo(switchProjectPath(location.pathname, routeSlug, project.slug)),
  }))

  // Same arrangement, and the same reason: the raw `name` an engineer would type
  // is the hint, the human `display_name` is the label, and both are matched.
  const eventTypeRows: PaletteRow[] = activeProject
    ? eventTypes.map(eventType => ({
        value: paletteValue.eventType(eventType.id),
        label: eventType.display_name,
        hint: eventType.name,
        icon: Tag,
        iconColor: eventType.color,
        onSelect: () => goTo(projectPath(currentOrgSlug(), activeProject.slug, `/events/${eventType.name}`)),
      }))
    : []

  // Switching branch swaps the data under the page without a navigation, so
  // it asks the page's unsaved-changes guard first and says the switch out
  // loud — the same as the sidebar's branch switcher (PL-1).
  const switchBranch = (targetId: string | null, targetName: string) =>
    runCommand(() =>
      requestPageLeave(() => {
        setBranchId(targetId)
        toast(
          targetId
            ? `Switched to ${targetName} — edits stay on this branch until it merges`
            : 'Switched to main — edits now change the live plan',
          { id: 'branch-switched' },
        )
      }),
    )

  // One "Switch to …" row per branch the reader is not on: main and every
  // open working branch. Before the switcher has loaded the list there is
  // nothing to name, so one row opens the branches page instead.
  const branchList = branchesQuery.data?.items
  const branchSwitchRows: PaletteRow[] = !activeProject
    ? []
    : branchList
      ? branchList
          .filter(branch =>
            branch.kind === 'main'
              ? branchId !== null && branchId !== branch.id
              : branch.id !== branchId && branch.status !== 'merged' && branch.status !== 'closed',
          )
          .map(branch => ({
            value: paletteValue.branch(branch.id),
            label: `Switch to ${branch.name}`,
            keywords: ['branch', 'switch', 'checkout'],
            icon: GitBranch,
            onSelect: () => switchBranch(branch.kind === 'main' ? null : branch.id, branch.name),
          }))
      : [
          // Its own value: the "Plan branches" jump row already owns
          // nav:<branches path>, and cmdk selects by value, so a shared one
          // would highlight both rows at once.
          {
            ...navRow(projectPath(currentOrgSlug(), activeProject.slug, '/branches'), 'Switch branch…', GitBranch, [
              'branch',
              'checkout',
            ]),
            value: paletteValue.action('switch-branch'),
          },
        ]

  // Commands, not places (#238 JR-19): a common task can be started from the
  // keyboard. Create actions only for a role that can create. "New branch" and
  // "Invite member" open their forms (`?new=1`, `?invite=1`), not just the page
  // the form lives on.
  const actionRows: PaletteRow[] = [
    ...(activeProject && canEdit
      ? [
          navRow(projectPath(currentOrgSlug(), activeProject.slug, '/events/all/new'), 'New event', Plus, ['create', 'add']),
          navRow(projectPath(currentOrgSlug(), activeProject.slug, '/metrics/new'), 'New metric', Plus, ['create', 'add']),
          navRow(projectPath(currentOrgSlug(), activeProject.slug, '/branches?new=1'), 'New branch', GitBranch, [
            'create',
            'add',
          ]),
        ]
      : []),
    ...branchSwitchRows,
    ...(isOwner
      ? [navRow(settingsPath('/settings/invitations?invite=1'), 'Invite member', UserPlus, ['add', 'user', 'invite'])]
      : []),
    {
      value: paletteValue.action('toggle-theme'),
      label: resolvedTheme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme',
      keywords: ['theme', 'dark', 'light', 'appearance', 'toggle'],
      icon: SunMoon,
      onSelect: () => runCommand(() => setTheme(resolvedTheme === 'dark' ? 'light' : 'dark')),
    },
  ]

  const accountRows: PaletteRow[] = [
    {
      value: paletteValue.account('sign-out'),
      label: 'Sign out',
      icon: LogOut,
      onSelect: () => runCommand(() => void auth.logout()),
    },
  ]

  // Two calls rather than one because the Account group is rendered BELOW the
  // knowledge results while the rest are above them, and the reading order is
  // part of the design.
  const menuGroups = visibleStaticGroups(query, [
    // The current project first, then the workspace (#238 SH-19): the list
    // used to open on Data sources, Members, Profile and Runtime.
    // Plan / Observe / Govern, in the sidebar's order and under the sidebar's
    // headings, so the palette reads as the same map of the product.
    ...projectNavGroups,
    ...(activeProject
      ? [{ heading: `More — ${activeProject.name}`, rows: projectExtraRows }]
      : []),
    { heading: 'Actions', rows: actionRows },
    { heading: 'Workspace', rows: navigateRows },
    { heading: 'Projects', rows: projectRows },
    ...(activeProject
      ? [{ heading: `Event types — ${activeProject.name}`, rows: eventTypeRows }]
      : []),
  ])
  const accountGroups = visibleStaticGroups(query, [
    { heading: 'Account', rows: accountRows },
  ])

  // Derived from the LIVE query, not the debounced one. A search is already
  // coming during the 200ms debounce window, and calling that window "no search
  // running" is what made the empty line flash on every query whose first
  // characters miss the static rows — those rows narrow against the live query,
  // so they are gone before the request is even sent.
  const knowledgeState: KnowledgeState = !searchSlug || query.trim().length < 2
    ? 'off'
    : query.trim() !== debouncedQuery || (searchQuery.isFetching && !lexicalSettled)
      ? 'searching'
      : searchQuery.isError && !lexicalSettled
        ? 'error'
        : searchResults.length > 0
          ? 'results'
          : 'empty'

  // The list's single "we found you nothing" line, decided in one place. It is
  // reachable only while the knowledge section is absent, so it can never stack
  // with that section's own empty line — the defect where a query matching no
  // static row and returning no results rendered "No matches." AND
  // "No knowledge matches." together.
  const showNoMatches =
    knowledgeState === 'off' &&
    menuGroups.length === 0 &&
    accountGroups.length === 0 &&
    !showAskAiAction
  // Outside a project there is no catalog to search; say so instead of
  // quietly searching one the reader did not pick.
  const showOpenProjectHint = !searchSlug && query.trim().length >= 2

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent
        showCloseButton={false}
        // Anchored near the top, not centred: centring moved the input ~180px
        // every time the result count changed, and the eye lost the caret
        // (#238 SH-19).
        className="top-[12vh] translate-y-0 overflow-hidden p-0 sm:max-w-[640px] gap-0"
        // Take over Radix's focus restore: it aims at whatever was focused when
        // the dialog mounted, which for a global Ctrl+K is usually <body>.
        onCloseAutoFocus={(event) => {
          event.preventDefault()
          onRestoreFocus()
        }}
        // In AI mode Esc means "back to search", as the key hint beside the
        // back button says; it used to close the whole palette (SHELL-28).
        onEscapeKeyDown={(event) => {
          if (!aiQuestion) return
          event.preventDefault()
          handleBackFromAi()
        }}
      >
        <DialogTitle className="sr-only">Command palette</DialogTitle>
        <Command
          label="Command palette"
          // Off, always. cmdk's filter is also a SORT — it re-appends every
          // rendered item in commandScore order on each keystroke — so leaving it
          // on discarded the backend's relevance ranking before anyone saw it
          // (tripl-k6gt). The static groups do their own filtering below; the
          // knowledge results are ranked and filtered server-side and must reach
          // the DOM untouched.
          shouldFilter={false}
          className="flex max-h-[480px] w-full min-w-0 flex-col"
        >
          <div
            className="flex items-center gap-2 border-b px-3.5 py-3 border-border-subtle"
          >
            {aiQuestion ? (
              <>
                <Sparkles className="h-3.5 w-3.5 shrink-0 text-fg-tertiary" />
                <span className="flex-1 truncate text-body text-fg">{aiQuestion}</span>
                <button
                  ref={aiBackRef}
                  type="button"
                  onClick={handleBackFromAi}
                  aria-keyshortcuts="Escape"
                  className="shrink-0 rounded-sm px-1.5 py-0.5 text-caption hover:bg-[var(--surface-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] text-fg-tertiary"
                >
                  ← Back to search
                </button>
                <Kbd>esc</Kbd>
              </>
            ) : (
              <>
                <Search className="h-3.5 w-3.5 text-fg-tertiary" />
                <Command.Input
                  // eslint-disable-next-line jsx-a11y/no-autofocus -- command palette search: focus on explicit ⌘K invocation is expected UX
                  autoFocus
                  value={query}
                  onValueChange={setQuery}
                  placeholder="Search projects, event types, events…"
                  className="flex-1 bg-transparent text-body outline-none placeholder:text-[var(--fg-subtle)]"
                />
                <Kbd>esc</Kbd>
              </>
            )}
          </div>

          {aiQuestion ? (
            <div className="flex-1 overflow-y-auto py-2 px-3.5" aria-live="polite">
              {askMutation.isPending && (
                <div className="flex items-center gap-2 py-2 text-body-sm text-fg-tertiary">
                  <Loader2 className="h-3 w-3 animate-spin" />
                  Asking AI…
                </div>
              )}
              {askMutation.isError && (
                <p className="py-2 text-body-sm text-destructive">
                  Error: {askMutation.error instanceof Error ? askMutation.error.message : 'Something went wrong'}
                </p>
              )}
              {aiResult && (
                <div className="space-y-3">
                  <p className="whitespace-pre-wrap text-body-sm leading-relaxed text-fg">
                    {aiResult.answer}
                  </p>
                  {aiResult.sources.length > 0 && (
                    <div className="space-y-1">
                      <p className="micro-label text-fg-tertiary">
                        Sources
                      </p>
                      {aiResult.sources.map((source, index) => (
                        <button
                          key={index}
                          type="button"
                          onClick={() => goToResult(source.route_path)}
                          className="flex w-full items-center gap-2 rounded-md px-2 py-1 text-left text-body-sm hover:bg-[var(--surface-hover)] text-fg"
                        >
                          <span className="shrink-0 text-micro tabular-nums text-fg-tertiary">
                            [{index + 1}]
                          </span>
                          <span className="flex-1 truncate">{source.title}</span>
                          <span className="shrink-0 text-micro text-fg-tertiary">
                            {source.entity_type}
                          </span>
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          ) : (
          <Command.List className="flex-1 overflow-y-auto py-1.5">
            {/* A plain div, deliberately NOT `Command.Empty`. cmdk's version
                renders on `filtered.count === 0`, and with `shouldFilter={false}`
                that count is just the number of REGISTERED items — which counts
                neither the "Searching knowledge…" group nor the failed-search
                line, since both hold a plain div and register nothing. So it
                reported "empty" while the user was looking at a spinner, and
                stacked underneath the knowledge section's own empty line.
                `showNoMatches` answers the question once, above. */}
            {showNoMatches && (
              <div className="px-3.5 py-8 text-center text-body-sm text-fg-tertiary">
                No matches.
              </div>
            )}
            {showOpenProjectHint && (
              <div className="px-3.5 py-2 text-caption text-fg-tertiary">
                Open a project to search its catalog and ask AI.
              </div>
            )}

            <StaticGroups groups={menuGroups} />

            {knowledgeState !== 'off' && (
              <>
                {/* All three status groups hold a plain div and register no cmdk
                    item. Before this commit that made them dead on arrival: with
                    the client filter on, a `Command.Group` with no MATCHING item
                    was hidden, and a query is the only way to reach any of these
                    branches — so nobody had seen a spinner or a "no knowledge
                    matches" line in the palette. With the filter off cmdk never
                    hides a group, and they render for the first time. */}
                {knowledgeState === 'error' ? (
                  // Says the request failed, and never "nothing found". An empty
                  // `items` array is what a failed request and an empty knowledge
                  // base used to have in common (`data?.items ?? []`), so the
                  // palette reported an outage as a fact about the user's data.
                  <Group heading="Search results">
                    <div
                      className="px-3.5 py-2 text-caption text-destructive"
                    >
                      Search failed. Results may be missing — try again.
                    </div>
                  </Group>
                ) : knowledgeState === 'empty' ? (
                  // "Search results", not "Knowledge matching …": the heading
                  // style uppercases, so the query was SHOUTED back, and
                  // "knowledge" is our word, not the reader's (#238 SH-20).
                  // The query sits in the line, in its own case.
                  <Group heading="Search results">
                    <div
                      className="px-3.5 py-2 text-caption text-fg-tertiary"
                    >
                      {menuGroups.length === 0 ? (
                        <>
                          No results for “{debouncedQuery}”. Try an event, metric or page name
                          {aiEnabled ? ', or ask AI.' : '.'}
                        </>
                      ) : (
                        <>No events, metrics or fields match “{debouncedQuery}”.</>
                      )}
                    </div>
                  </Group>
                ) : searchGroups.length === 0 ? (
                  // Only reachable while searching with nothing this session may
                  // keep — its first query, or the first after the input was
                  // cleared. Once there are rows, the branch below keeps them
                  // and says so, rather than emptying the dialog for the length
                  // of the round trip.
                  <Group heading="Searching…">
                    <div
                      className="px-3.5 py-2 text-caption text-fg-tertiary"
                    >
                      Searching.
                    </div>
                  </Group>
                ) : (
                  // Rendered exactly as received: no `.filter`, no `.sort` and no
                  // `matchesQuery` within each entity-type bucket — these rows
                  // were already matched and ranked by the search service, and
                  // second-guessing that here is the defect this commit removes
                  // (tripl-k6gt). What bucketing costs ACROSS buckets is spelled
                  // out on `groupSearchResults`.
                  //
                  // While the next search is in flight these are the PREVIOUS
                  // query's rows (`placeholderData` above), dimmed under an
                  // in-flight line: the list narrows instead of blinking empty,
                  // and cmdk keeps its selection. They stay selectable on
                  // purpose — Enter goes to what the reader can see (tripl-2x5d).
                  <div
                    aria-busy={knowledgeState === 'searching'}
                    style={
                      knowledgeState === 'searching' ? { opacity: STALE_RESULT_OPACITY } : undefined
                    }
                  >
                    {searchGroups.map(([entityType, results]) => {
                      const meta = SEARCH_TYPE_META[entityType]
                      return (
                        <Group key={entityType} heading={meta.heading}>
                          {results.map(result => {
                            const isEvent = result.entity_type === 'event'
                            const eventType = isEvent
                              ? eventTypes.find(item => item.display_name === result.subtitle)
                              : undefined
                            // Scoped to events on purpose (tripl-wkwv.5): an
                            // event's search title is its stored name and the
                            // catalog holds blank ones, so the row rendered as
                            // an icon and nothing else. Every other entity's
                            // title is a display_name, a #tag, a ${variable} or
                            // a scan-config name — non-empty by schema, so
                            // "(unnamed event)" on one of those would be a lie.
                            const label = isEvent ? eventNameLabel(result.title) : result.title
                            const group = result.variant_group
                            const row = (
                              <Item
                                key={result.id}
                                value={paletteValue.search(result.id)}
                                onSelect={() => goToResult(result.route_path)}
                                icon={meta.icon}
                                iconColor={eventType?.color}
                                label={label}
                                variants={group?.variants.length}
                                hint={result.subtitle || undefined}
                                description={result.description || result.snippet || undefined}
                                semantic={result.semantic_used}
                              />
                            )
                            if (!group) return row
                            // Folded server-side (#238 JR-20). Keyed by the
                            // group, so an expanded group stays open when the
                            // full answer replaces the keyword-only one.
                            return (
                              <SearchVariantRows
                                key={group.key}
                                group={group}
                                representative={row}
                                toggleValue={paletteValue.variants(group.key)}
                                renderVariant={variant => (
                                  <Item
                                    key={variant.id}
                                    value={paletteValue.search(variant.id)}
                                    onSelect={() => goToResult(variant.route_path)}
                                    icon={meta.icon}
                                    iconColor={eventType?.color}
                                    label={eventNameLabel(variant.title)}
                                    hint={variant.value || undefined}
                                  />
                                )}
                              />
                            )
                          })}
                        </Group>
                      )
                    })}
                    {knowledgeState === 'searching' && (
                      <div
                        className="flex items-center gap-2 px-3.5 py-2 text-caption text-fg-tertiary"
                      >
                        <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
                        Updating results…
                      </div>
                    )}
                  </div>
                )}
              </>
            )}

            <StaticGroups groups={accountGroups} />

            {showAskAiAction && (
              // Deliberately NOT run through `matchesQuery`. This row is the
              // query, so matching it against the query is either a tautology or,
              // during the debounce window, false — the label still quotes the
              // previous `debouncedQuery` while `query` has moved on, so the offer
              // would blink out mid-word on every keystroke. `showAskAiAction` is
              // already its visibility rule.
              <Group heading="AI">
                <Item
                  value={paletteValue.ai()}
                  onSelect={() => handleAskAi(debouncedQuery)}
                  icon={Sparkles}
                  label={`Ask AI: «${debouncedQuery}»`}
                />
              </Group>
            )}
          </Command.List>
          )}
        </Command>
      </DialogContent>
    </Dialog>
  )
}

function Group({ heading, children }: { heading: string; children: ReactNode }) {
  return (
    <Command.Group
      heading={heading}
      className="px-1.5 py-1 [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:pt-1.5 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:micro-label [&_[cmdk-group-heading]]:text-[var(--fg-faint)]"
    >
      {children}
    </Command.Group>
  )
}

/**
 * Renders already-narrowed groups. Every group it is handed has at least one
 * row, because `visibleStaticGroups` dropped the empty ones — see there for why
 * an empty group must not reach the DOM at all under `shouldFilter={false}`.
 */
function StaticGroups({ groups }: { groups: PaletteGroup[] }) {
  return (
    <>
      {groups.map(group => (
        <Group key={group.heading} heading={group.heading}>
          {group.rows.map(row => (
            <Item key={row.value} {...row} />
          ))}
        </Group>
      ))}
    </>
  )
}

function Item({
  value,
  onSelect,
  icon: Icon,
  iconColor,
  label,
  hint,
  description,
  semantic,
  active,
  variants,
}: PaletteRow & {
  description?: string
  semantic?: boolean
  /** Folded variants behind this search row (#238 JR-20). */
  variants?: number
}) {
  // No "80%" badge (#238 JR-20): the exact event and eight scan variants all
  // read 80%, so the list looked like duplicates, and the figure meant nothing
  // to a reader. The server's order already says which is best.
  return (
    <Command.Item
      value={value}
      onSelect={onSelect}
      className={PALETTE_ITEM_CLASS}
    >
      <Icon
        className="h-3.5 w-3.5 shrink-0 self-start mt-0.5"
        style={{ color: iconColor ?? 'var(--fg-subtle)' }}
      />
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="truncate">
          {label}
          {variants ? <SearchVariantCount count={variants} /> : null}
        </span>
        {description && (
          <span className="truncate text-micro text-fg-tertiary">
            {description}
          </span>
        )}
      </span>
      {/* The narrowed rule (tripl-wkwv.3): the chip marks a row the keyword
          ranking did not surface, so a row whose name IS what you typed does
          not carry it. The old wording claimed every hybrid hit.
          Not "no keyword matched this": the keyword leg is a LIMIT-ed scan, so
          a weak match displaced from its window re-enters through the meaning
          leg and would wear that claim while containing the typed word. */}
      {semantic && (
        <Chip
          tone="neutral"
          size="xs"
          title="Found by meaning — the keyword ranking didn't surface this"
        >
          semantic
        </Chip>
      )}
      {active && (
        <span className="shrink-0 micro-label text-fg-tertiary">
          current
        </span>
      )}
      {hint && (
        <span
          className="mono hidden max-w-[40%] shrink-0 truncate text-micro sm:block text-fg-tertiary"
        >
          {hint}
        </span>
      )}
    </Command.Item>
  )
}
