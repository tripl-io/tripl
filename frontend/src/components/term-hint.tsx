import { Info } from 'lucide-react'
import { Link } from 'react-router-dom'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { termAnchor } from '@/lib/glossary'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

/**
 * A small info icon beside a term a PM may not know ("Coverage",
 * "Reconciliation", "Fact table"): hover or focus shows the glossary's
 * one-line definition, and a click opens the term's row on the Concepts page
 * (#238). Meant for a PageHeader `titleAddon`.
 *
 * `term` must be the glossary's own spelling, since the anchor is derived from
 * it; `definition` is the line to show, kept short. It carries its own
 * TooltipProvider, as IconButton does, so a page renders it outside the app's
 * root provider too (page tests mount without one).
 */
export function TermHint({
  slug,
  term,
  definition,
}: {
  slug: string
  term: string
  definition: string
}) {
  return (
    <TooltipProvider delayDuration={300}>
      <Tooltip>
        <TooltipTrigger asChild>
          <Link
            to={projectPath(currentOrgSlug(), slug, `/concepts#${termAnchor(term)}`)}
            aria-label={`What is ${term}? Open in Concepts`}
            className="inline-flex items-center rounded-sm text-fg-tertiary transition-colors hover:text-fg focus-visible:text-fg"
          >
            <Info className="size-3.5" aria-hidden="true" />
          </Link>
        </TooltipTrigger>
        <TooltipContent className="max-w-xs">{definition}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}
