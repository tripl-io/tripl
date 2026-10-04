/**
 * The chapter picker: every scenario chapter with a status chip,
 * one click to start or resume it. Shared between the product tour's hands-on
 * block and the demo welcome panel — presentation only, the caller owns what
 * "pick" means (start + navigate + close whatever hosted the picker).
 */

import { Check, Play, RotateCcw } from 'lucide-react'
import { Chip, type ChipTone } from '@/components/primitives/chip'
import type { ChapterListEntry, ChapterStatus } from './scenarioModel'

const STATUS_LABEL: Record<Exclude<ChapterStatus, 'not_started'>, string> = {
  active: 'Active',
  completed: 'Completed',
  dismissed: 'Paused',
}

const STATUS_TONE: Record<Exclude<ChapterStatus, 'not_started'>, ChipTone> = {
  active: 'accent',
  completed: 'success',
  dismissed: 'neutral',
}

function StatusIcon({ status }: { status: ChapterStatus }) {
  if (status === 'completed') return <Check className="h-3 w-3" aria-hidden="true" />
  if (status === 'active' || status === 'dismissed') {
    return <RotateCcw className="h-3 w-3" aria-hidden="true" />
  }
  return <Play className="h-3 w-3" aria-hidden="true" />
}

interface ChapterPickerProps {
  chapters: ChapterListEntry[]
  onPick: (chapter: ChapterListEntry) => void
  /** Compact rows (welcome panel) vs full rows with blurbs (tour). */
  compact?: boolean
}

export function ChapterPicker({ chapters, onPick, compact = false }: ChapterPickerProps) {
  return (
    <ol className="flex min-w-0 flex-col gap-1" aria-label="Scenario chapters">
      {chapters.map((chapter, index) => (
        <li key={chapter.id} className="min-w-0">
          <button
            type="button"
            onClick={() => onPick(chapter)}
            // Compact rows are bare titles, and picking one is not a preview —
            // it starts the chapter and navigates away, reassigning the active
            // chapter the strip may be mid-way through. The tooltip lets the
            // user read what a chapter teaches before that commit, at zero
            // added height on the surface tripl-wnzi is trying to shorten.
            title={compact ? chapter.blurb : undefined}
            className="flex w-full min-w-0 items-center gap-2 rounded-md border px-2.5 py-1.5 text-left transition-colors hover:bg-[var(--surface-hover)]"
            style={{ background: 'var(--surface)', borderColor: 'var(--border-subtle)' }}
          >
            <span
              className="tnum w-4 shrink-0 text-center text-micro text-fg-tertiary"
              aria-hidden="true"
            >
              {index + 1}
            </span>
            <span className="flex shrink-0 items-center text-accent">
              <StatusIcon status={chapter.status} />
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-body-sm font-medium">{chapter.title}</span>
              {!compact && (
                // Whole, not cut to ~25 characters on a phone (#251 SH-7):
                // it wraps from `sm` up, and a phone shows the titles alone.
                <span
                  className="hidden text-micro sm:block text-fg-tertiary"
                >
                  {chapter.blurb}
                </span>
              )}
            </span>
            {/* A row that has not been started is the default and says
                nothing: seven "Not started" pills drowned the one that
                matters (#251 SH-3). */}
            {chapter.status !== 'not_started' && (
              <Chip tone={STATUS_TONE[chapter.status]} size="xs">
                {STATUS_LABEL[chapter.status]}
              </Chip>
            )}
          </button>
        </li>
      ))}
    </ol>
  )
}
