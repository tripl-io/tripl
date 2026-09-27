import { useId, useRef, useState, type KeyboardEvent } from 'react'
import { AnchoredListbox } from '@/components/ui/anchored-listbox'
import {
  activeMentionQuery,
  filterMentionCandidates,
  insertMention,
  type MentionCandidate,
} from '@/lib/mentions'
import { cn } from '@/lib/utils'

/**
 * The comment textarea with @ autocomplete (#259). Typing `@` and a few letters
 * lists the project's members; picking one writes `@[Name](user_id)` into the
 * body, which the thread then draws as a chip and the server turns into a
 * notification for that member. Without `candidates` it is a plain textarea.
 *
 * Enter belongs to the text (a comment is often several lines) except while
 * the list is open, where it picks; Cmd/Ctrl+Enter posts.
 */
export function MentionComposer({
  id,
  value,
  onChange,
  onSubmit,
  candidates,
  placeholder,
  className,
}: {
  id: string
  value: string
  onChange: (value: string) => void
  onSubmit: () => void
  candidates?: readonly MentionCandidate[]
  placeholder?: string
  className?: string
}) {
  const listId = useId()
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)
  const [mention, setMention] = useState<{ start: number; query: string } | null>(null)
  const [highlight, setHighlight] = useState(0)

  const options = mention && candidates ? filterMentionCandidates(candidates, mention.query) : []
  const open = options.length > 0
  const activeIndex = Math.min(highlight, Math.max(options.length - 1, 0))

  const track = (text: string, caret: number | null) => {
    const next = caret === null || !candidates ? null : activeMentionQuery(text, caret)
    if (next?.start !== mention?.start || next?.query !== mention?.query) setHighlight(0)
    setMention(next)
  }

  const pick = (candidate: MentionCandidate) => {
    const el = textareaRef.current
    if (!mention || !el) return
    const caret = el.selectionStart ?? value.length
    const next = insertMention(value, mention.start, caret, candidate.name, candidate.userId)
    onChange(next.text)
    setMention(null)
    requestAnimationFrame(() => {
      el.focus()
      el.setSelectionRange(next.caret, next.caret)
    })
  }

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (open) {
      if (event.key === 'ArrowDown') {
        event.preventDefault()
        setHighlight((activeIndex + 1) % options.length)
        return
      }
      if (event.key === 'ArrowUp') {
        event.preventDefault()
        setHighlight((activeIndex - 1 + options.length) % options.length)
        return
      }
      if ((event.key === 'Enter' && !event.metaKey && !event.ctrlKey) || event.key === 'Tab') {
        const chosen = options[activeIndex]
        if (chosen) {
          event.preventDefault()
          pick(chosen)
          return
        }
      }
      if (event.key === 'Escape') {
        event.preventDefault()
        setMention(null)
        return
      }
    }
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
      event.preventDefault()
      onSubmit()
    }
  }

  return (
    <>
      <textarea
        ref={textareaRef}
        id={id}
        value={value}
        onChange={event => {
          onChange(event.target.value)
          track(event.target.value, event.target.selectionStart)
        }}
        onSelect={event => track(event.currentTarget.value, event.currentTarget.selectionStart)}
        onBlur={() => setMention(null)}
        onKeyDown={onKeyDown}
        // A textbox with list autocomplete whenever it offers members. Not
        // role="combobox": ARIA in HTML allows no role on a <textarea>
        // (axe aria-allowed-role), and a textbox takes no aria-expanded — the
        // status region below announces the list opening instead. The popup
        // it owns is named even while closed.
        aria-haspopup={candidates ? 'listbox' : undefined}
        aria-autocomplete={candidates ? 'list' : undefined}
        aria-controls={candidates ? listId : undefined}
        aria-activedescendant={open ? `${listId}-opt-${activeIndex}` : undefined}
        placeholder={placeholder}
        className={className}
      />
      {candidates && (
        // Says how many members match while the list is open; a screen reader
        // does not announce a popup appearing under the caret on its own.
        <span role="status" aria-live="polite" className="sr-only">
          {open ? `${options.length} ${options.length === 1 ? 'member' : 'members'}` : ''}
        </span>
      )}
      <AnchoredListbox
        id={listId}
        open={open}
        anchorRef={textareaRef}
        onDismiss={() => setMention(null)}
        ariaLabel="Mention a member"
        className="w-64"
      >
        {options.map((candidate, index) => (
          <button
            key={candidate.userId}
            id={`${listId}-opt-${index}`}
            type="button"
            role="option"
            tabIndex={-1}
            aria-selected={index === activeIndex}
            // mousedown, not click: the textarea must not blur first.
            onMouseDown={event => {
              event.preventDefault()
              pick(candidate)
            }}
            className={cn(
              'flex w-full flex-col items-start rounded-sm px-2 py-1.5 text-left text-body-sm',
              index === activeIndex ? 'bg-surface-hover text-foreground' : 'text-popover-foreground hover:bg-surface-hover',
            )}
          >
            <span className="w-full truncate font-medium">{candidate.name}</span>
            <span className="w-full truncate text-micro text-fg-tertiary">{candidate.email}</span>
          </button>
        ))}
      </AnchoredListbox>
    </>
  )
}
