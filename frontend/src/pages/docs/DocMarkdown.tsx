import { useMemo, type ComponentProps, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import Markdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { AlertTriangle, ExternalLink, FileX, Unlink } from 'lucide-react'
import {
  describeSuggestions,
  describeUnresolved,
  docLinkRoute,
  indexResolutions,
  isIdKind,
  isUnavailableNote,
  isUuid,
  mentionName,
  parseDocLinkHref,
  remarkDocLinks,
  resolutionKey,
  resolveRelativeDocHref,
  type ParsedDocLinkHref,
} from '@/lib/docLinks'
import { docRoute } from '@/lib/docTree'
import { DOC_LINK_KIND_ICON, INLINE_ICON_KINDS } from './docLinkIcons'
import { docUrlTransform } from './docMarkdownUrl'
import { cn } from '@/lib/utils'
import type { DocLinkResolution, DocScope } from '@/types/docs'
import { withActiveOrg } from '@/lib/navigation'

/**
 * Renders a note's Markdown body (F22). GFM (tables, task lists,
 * strikethrough) is on; raw HTML is NOT — there is no rehype-raw, so a
 * `<script>` or `<img onerror>` in a note prints as text. That is the XSS
 * stance for content any editor (or an imported bundle) can write.
 *
 * `[[event:…]]`-style links become in-app links through `resolutions`; a
 * broken one renders as a red chip, a link to a note the reader cannot see
 * as a generic "unavailable note", and `[[user:<id>]]` as `@Name`. Relative `.md` links open the sibling
 * note in the same scope. Images are shown as links, never fetched: the
 * catalog is Markdown only, and a remote image in a note is a tracking pixel.
 */
export function DocMarkdown({
  body,
  slug,
  scope,
  path,
  resolutions,
  className,
}: {
  body: string
  slug: string
  scope: DocScope
  /** The note's own path, for relative links. */
  path: string
  /** `undefined` while resolutions are loading: links render neutral. */
  resolutions: readonly DocLinkResolution[] | undefined
  className?: string
}) {
  const index = useMemo(() => (resolutions ? indexResolutions(resolutions) : null), [resolutions])

  const components = useMemo<Components>(
    () => ({
      a: ({ href, children, node: _node, ...rest }) => {
        void _node
        return (
          <DocAnchor href={href} slug={slug} scope={scope} path={path} index={index} rest={rest}>
            {children}
          </DocAnchor>
        )
      },
      img: ({ src, alt }) => {
        const url = typeof src === 'string' ? src : ''
        const label = alt || url || 'image'
        return /^https?:/i.test(url) ? (
          <a href={url} target="_blank" rel="noopener noreferrer nofollow" className="doc-md-link">
            [image: {label}]
          </a>
        ) : (
          <span className="text-fg-tertiary">[image: {label}]</span>
        )
      },
    }),
    [slug, scope, path, index],
  )

  return (
    <div data-slot="doc-markdown" className={cn(MARKDOWN_CLASS, className)}>
      <Markdown remarkPlugins={[remarkGfm, remarkDocLinks]} urlTransform={docUrlTransform} components={components}>
        {body}
      </Markdown>
    </div>
  )
}

const LINK_CLASS = 'text-[var(--accent)] underline decoration-[var(--accent-soft)] underline-offset-2 hover:decoration-[var(--accent)]'

/**
 * What a `[[…]]` link shows: the author's `|label` when there is one; for a
 * link by id, the target's CURRENT name from the server (a moved note shows
 * its new title, a mention `@Name`); otherwise the name as written.
 */
function linkText(entity: ParsedDocLinkHref, resolution: DocLinkResolution | undefined, written: ReactNode): ReactNode {
  if (entity.kind === 'user') {
    const name = mentionName(entity.label ?? resolution?.label)
    return name ? `@${name}` : '@someone'
  }
  if (entity.label || !isIdKind(entity.kind)) return written
  if (resolution?.label) return resolution.label
  if (entity.kind === 'doc') return isUuid(entity.target) ? 'Linked note' : written
  return 'Alert rule'
}

function DocAnchor({
  href,
  slug,
  scope,
  path,
  index,
  rest,
  children,
}: {
  href: string | undefined
  slug: string
  scope: DocScope
  path: string
  index: Map<string, DocLinkResolution> | null
  rest: Omit<ComponentProps<'a'>, 'href' | 'children'>
  children: ReactNode
}) {
  const entity = parseDocLinkHref(href)
  if (entity) {
    const resolution = index?.get(resolutionKey(entity.kind, entity.target, entity.qualifier))
    const Icon = INLINE_ICON_KINDS.has(entity.kind) ? DOC_LINK_KIND_ICON[entity.kind] : null
    const icon = Icon ? <Icon className="mr-0.5 inline size-3 shrink-0 align-[-1px]" aria-hidden /> : null
    const mono = entity.kind !== 'user' && !isIdKind(entity.kind)
    if (!resolution) {
      return (
        <span
          data-doc-link="pending"
          data-doc-link-kind={entity.kind}
          className={cn('rounded-sm bg-bg-sunken px-1', mono && 'font-mono')}
        >
          {icon}
          {linkText(entity, undefined, children)}
        </span>
      )
    }
    if (isUnavailableNote(resolution)) {
      // Deleted or hidden from this reader: the same words either way, and no
      // label — the chip must not reveal that a hidden note exists or its name.
      return (
        <span
          data-doc-link="unavailable"
          data-doc-link-kind="doc"
          title="This note is unavailable"
          className="inline-flex items-center gap-1 rounded-sm bg-bg-sunken px-1 text-fg-tertiary"
        >
          <FileX className="size-3 shrink-0" aria-hidden />
          Unavailable note
        </span>
      )
    }
    if (entity.kind === 'user' && resolution.status === 'resolved' && !resolution.route_path) {
      // A mention has no page of its own to open: a plain @Name chip.
      return (
        <span
          data-doc-link="resolved"
          data-doc-link-kind="user"
          className="rounded-sm bg-[var(--accent-soft)] px-1 font-medium text-[var(--accent)]"
        >
          {linkText(entity, resolution, children)}
        </span>
      )
    }
    if ((resolution.status !== 'resolved' && resolution.status !== 'ambiguous') || !resolution.route_path) {
      // A title, not a Radix tooltip: the renderer also runs in the editor
      // preview and in tests without the app's TooltipProvider.
      const hint = describeSuggestions(resolution)
      const reason = hint ? `${describeUnresolved(resolution)} ${hint}` : describeUnresolved(resolution)
      return (
        <span
          data-doc-link="broken"
          data-doc-link-kind={entity.kind}
          title={reason}
          className={cn(
            'inline-flex items-center gap-1 rounded-sm bg-danger-soft px-1 text-danger line-through decoration-1',
            mono && 'font-mono',
          )}
        >
          <Unlink className="size-3 shrink-0" aria-hidden />
          {linkText(entity, resolution, children)}
          <span className="sr-only"> (broken link: {reason})</span>
        </span>
      )
    }
    const ambiguous = resolution.status === 'ambiguous'
    const route = docLinkRoute(resolution.route_path, entity.anchor)
    return (
      <Link
        to={withActiveOrg(route)}
        data-doc-link={resolution.status}
        data-doc-link-kind={entity.kind}
        title={ambiguous ? describeUnresolved(resolution) : undefined}
        className={cn(LINK_CLASS, mono && 'font-mono', ambiguous && 'text-warning')}
      >
        {icon}
        {linkText(entity, resolution, children)}
        {ambiguous && <AlertTriangle className="ml-0.5 inline size-3 align-[-1px]" aria-label="ambiguous" />}
      </Link>
    )
  }

  const relative = resolveRelativeDocHref(path, href)
  if (relative) {
    return (
      <Link to={`${docRoute(slug, scope, relative.path)}${relative.hash}`} className={LINK_CLASS}>
        {children}
      </Link>
    )
  }

  if (href && /^(https?:|mailto:)/i.test(href)) {
    return (
      <a {...rest} href={href} target="_blank" rel="noopener noreferrer nofollow" className={LINK_CLASS}>
        {children}
        {/^https?:/i.test(href) && <ExternalLink className="ml-0.5 inline size-3 align-[-1px]" aria-hidden />}
      </a>
    )
  }

  return (
    <a {...rest} href={href || undefined} className={LINK_CLASS}>
      {children}
    </a>
  )
}

/**
 * Prose styles, token colours only (raw-palette.test.ts). Scoped with
 * descendant selectors because the Markdown elements are generated.
 */
const MARKDOWN_CLASS = cn(
  'min-w-0 break-words text-body leading-[1.65] text-fg',
  '[&>*:first-child]:mt-0 [&>*:last-child]:mb-0',
  '[&_h1]:mb-3 [&_h1]:mt-6 [&_h1]:text-title [&_h1]:font-semibold [&_h1]:tracking-[-0.01em]',
  '[&_h2]:mb-2 [&_h2]:mt-6 [&_h2]:border-b [&_h2]:border-border [&_h2]:pb-1 [&_h2]:text-heading [&_h2]:font-semibold',
  '[&_h3]:mb-2 [&_h3]:mt-5 [&_h3]:font-semibold',
  '[&_h4]:mb-1 [&_h4]:mt-4 [&_h4]:font-semibold [&_h5]:font-semibold [&_h6]:font-semibold',
  '[&_p]:my-3 [&_ul]:my-3 [&_ul]:list-disc [&_ul]:pl-6 [&_ol]:my-3 [&_ol]:list-decimal [&_ol]:pl-6',
  '[&_li]:my-1 [&_li>p]:my-1 [&_.contains-task-list]:list-none [&_.contains-task-list]:pl-1',
  '[&_blockquote]:my-3 [&_blockquote]:border-l-2 [&_blockquote]:border-border [&_blockquote]:pl-3 [&_blockquote]:text-fg-secondary',
  '[&_code]:rounded-sm [&_code]:bg-bg-sunken [&_code]:px-1 [&_code]:font-mono',
  '[&_pre]:my-3 [&_pre]:overflow-x-auto [&_pre]:rounded-control [&_pre]:border [&_pre]:border-border [&_pre]:bg-bg-sunken [&_pre]:p-3',
  '[&_pre_code]:bg-transparent [&_pre_code]:p-0',
  '[&_table]:my-3 [&_table]:block [&_table]:max-w-full [&_table]:overflow-x-auto [&_table]:border-collapse',
  '[&_th]:border [&_th]:border-border [&_th]:bg-bg-sunken [&_th]:px-2 [&_th]:py-1 [&_th]:text-left [&_th]:font-semibold',
  '[&_td]:border [&_td]:border-border [&_td]:px-2 [&_td]:py-1',
  '[&_hr]:my-6 [&_hr]:border-border',
)
