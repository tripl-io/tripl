import { Link, useParams } from 'react-router-dom'
import { Plus } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { PageHeader } from '@/components/primitives/page-header'
import { PageContainer } from '@/components/primitives/page-container'
import { TermHint, TERM_HINTS } from '@/components/term-hint'
import { FactTablesList } from '@/pages/fact-tables/FactTablesList'
import { MetricsCatalog } from './MetricsCatalog'
import { useCanWriteProject } from '@/lib/permissions'
import { ReadOnlyNotice } from '@/components/states'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

export type MetricsTab = 'catalog' | 'fact-tables'

const TABS: { id: MetricsTab; label: string; path: (slug: string) => string }[] = [
  { id: 'catalog', label: 'Catalog', path: slug => projectPath(currentOrgSlug(), slug, '/metrics') },
  { id: 'fact-tables', label: 'Fact tables', path: slug => projectPath(currentOrgSlug(), slug, '/metrics/fact-tables') },
]

/**
 * Metrics surface (area "Observe"). The header + tab bar are URL-driven: each
 * tab is its own route (`/metrics` and `/metrics/fact-tables`), so tabs are
 * deep-linkable and browser back/forward switches them. Fact tables exist only
 * to back fact metrics, so they live here as a tab rather than as a standalone
 * sidebar entry. The body and the primary action are contextual to the active
 * tab; the H1 stays "Metrics" so the breadcrumb reads "Observe › Metrics".
 */
export default function MetricsPage({ tab = 'catalog' }: { tab?: MetricsTab }) {
  const { slug } = useParams<{ slug: string }>()
  const canWrite = useCanWriteProject()

  // Creating a metric or fact table is an editor's job; a viewer gets the
  // catalog and one line saying why there is no New button.
  const action = !canWrite ? undefined :
    slug && tab === 'fact-tables' ? (
      <Button asChild size="sm">
        <Link to={projectPath(currentOrgSlug(), slug, '/metrics/fact-tables/new')} className="no-underline">
          <Plus className="h-3.5 w-3.5" />
          {/* "New table" on a phone, so the button stays beside the title
              as "New metric" does instead of wrapping under it. */}
          New <span className="max-sm:hidden">fact </span>table
        </Link>
      </Button>
    ) : slug ? (
      <Button asChild size="sm">
        <Link to={projectPath(currentOrgSlug(), slug, '/metrics/new')} className="no-underline">
          <Plus className="h-3.5 w-3.5" />
          New metric
        </Link>
      </Button>
    ) : undefined

  return (
    <PageContainer>
      <div className="space-y-4">
        {/* Fact tables are a panel, not the page's title, so the hint rides the
            Metrics header while their tab is open (#238). */}
        <PageHeader
          eyebrow="Observe"
          title="Metrics"
          titleAddon={slug && tab === 'fact-tables' && <TermHint slug={slug} {...TERM_HINTS.factTables} />}
          actions={action}
        />
        <MetricsTabs slug={slug} tab={tab} />
      </div>

      {!canWrite && <ReadOnlyNotice />}

      {tab === 'fact-tables' ? <FactTablesList slug={slug} /> : <MetricsCatalog slug={slug} />}
    </PageContainer>
  )
}

// Route links, so a `<nav>` of links with `aria-current="page"` rather than a
// tablist: the tab roles promised arrow-key roving and tabpanels that links
// never had, and screen readers announced a widget that did not behave like one.
function MetricsTabs({ slug, tab }: { slug?: string; tab: MetricsTab }) {
  return (
    <nav
      aria-label="Metrics sections"
      className="flex gap-1 border-b border-border"
    >
      {TABS.map(t => {
        const active = t.id === tab
        return (
          <Link
            key={t.id}
            to={slug ? t.path(slug) : '#'}
            aria-current={active ? 'page' : undefined}
            className="-mb-px px-3 py-2 text-body-sm font-medium no-underline transition-colors"
            style={{
              color: active ? 'var(--fg)' : 'var(--fg-muted)',
              borderBottom: active ? '2px solid var(--accent)' : '2px solid transparent',
            }}
          >
            {t.label}
          </Link>
        )
      })}
    </nav>
  )
}
