import { Link, useLocation } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'
import { Chip } from '@/components/primitives/chip'
import { parseOnboardingReturn } from '@/components/onboarding-steps'
import { projectHomePath } from '@/lib/navigation'

/**
 * The way back from a getting-started step (#250). A step link opens its
 * page (data sources, scans, the review queue…) tagged with `?onboarding=…`;
 * on that page this slim bar says which step it is and leads back to the
 * checklist on the project's Overview. Without the tag it renders nothing, so
 * it can sit above every page.
 */
export function OnboardingReturnBar({ className = '' }: { className?: string }) {
  const { pathname, search } = useLocation()
  const target = parseOnboardingReturn(pathname, search)
  if (!target) return null
  return (
    <nav
      aria-label="Getting started"
      className={`flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-card border border-border-subtle bg-bg-sunken px-3 py-2 ${className}`}
    >
      <Chip tone="info" size="sm">{`Step ${target.number} of ${target.total}`}</Chip>
      <span className="min-w-0 flex-1 truncate text-body-sm">
        <span className="text-fg-tertiary">Getting started ·</span>{' '}
        <span className="font-medium">{target.title}</span>
      </span>
      <Link
        to={projectHomePath(target.slug)}
        className="flex shrink-0 items-center gap-1 text-caption font-medium no-underline hover:underline text-accent"
      >
        <ArrowLeft aria-hidden="true" className="h-3 w-3" />
        Back to checklist
      </Link>
    </nav>
  )
}
