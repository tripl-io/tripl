import { Bookmark, Check, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { IconButton } from '@/components/ui/icon-button'
import { Input } from '@/components/ui/input'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import type { EventsSavedView } from './savedViews'

export function SavedViewsMenu({
  views,
  activeViewName,
  draftName,
  onDraftNameChange,
  onSave,
  onApply,
  onDelete,
}: {
  views: EventsSavedView[]
  activeViewName: string | null
  draftName: string
  onDraftNameChange: (value: string) => void
  onSave: () => void
  onApply: (view: EventsSavedView) => void
  onDelete: (name: string) => void
}) {
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button variant="outline" size="sm">
          <Bookmark />
          <span className="max-sm:sr-only">Views</span>
          {activeViewName && (
            <span className="max-w-24 truncate text-micro text-fg-tertiary max-sm:hidden">
              {activeViewName}
            </span>
          )}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-72 p-2">
        <div
          className="px-1 pb-2 micro-label text-fg-tertiary"
        >
          Saved views
        </div>
        <div className="mb-2 flex gap-1">
          <Input
            aria-label="Saved view name"
            value={draftName}
            onChange={event => onDraftNameChange(event.target.value)}
            onKeyDown={event => {
              if (event.key === 'Enter') onSave()
            }}
            placeholder="Name this view…"
          />
          {/* A labelled button, not a lone floppy-disk icon. */}
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-8 shrink-0"
            onClick={onSave}
            disabled={!draftName.trim()}
          >
            Save
          </Button>
        </div>

        {views.length === 0 ? (
          // Says what a view keeps and who sees it: views live in this
          // browser's storage (savedViews.ts), so they are personal.
          <p className="px-1 py-1 text-caption text-fg-tertiary">
            Save the current search, filters and sort to come back to them. Views are kept in this
            browser and only you see them.
          </p>
        ) : (
          <div className="max-h-64 overflow-auto">
            {views.map(view => {
              const isActive = view.name === activeViewName
              return (
                <div
                  key={view.name}
                  className="flex items-center gap-1 rounded-sm px-1 py-1 hover:bg-[var(--surface-hover)]"
                >
                  <button
                    type="button"
                    aria-pressed={isActive}
                    onClick={() => onApply(view)}
                    className="flex min-w-0 flex-1 items-center gap-2 rounded-sm px-1 py-1 text-left text-body-sm"
                  >
                    <span className="inline-flex h-3.5 w-3.5 shrink-0 items-center justify-center" aria-hidden="true">
                      {isActive && <Check className="h-3 w-3" />}
                    </span>
                    <span className="min-w-0 flex-1 truncate">{view.name}</span>
                    <span className="shrink-0 text-micro text-fg-tertiary">{view.tab}</span>
                  </button>
                  <IconButton
                    type="button"
                    variant="ghost"
                    className="h-7 w-7 text-fg-tertiary"
                    onClick={() => onDelete(view.name)}
                    label={`Delete saved view ${view.name}`}
                  >
                    <Trash2 className="h-3 w-3" />
                  </IconButton>
                </div>
              )
            })}
          </div>
        )}
      </PopoverContent>
    </Popover>
  )
}
