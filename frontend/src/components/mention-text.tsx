import { Chip } from '@/components/primitives/chip'
import { parseMentions } from '@/lib/mentions'
import { cn } from '@/lib/utils'

/** Who a mentioned user id is now, keyed by the lower-case id. */
export type MentionPeople = ReadonlyMap<string, { name: string; email: string }>

/**
 * A comment body with its `@[Name](user_id)` tokens drawn as chips (#259).
 * Everything else stays text: React escapes it, and line breaks are kept by
 * the caller's `whitespace-pre-wrap`.
 *
 * A chip shows the person's current name when `people` knows the id, so a
 * rename reaches old comments; otherwise the name stored in the token. Its
 * title carries the email when known.
 */
export function MentionText({
  body,
  people,
  className,
}: {
  body: string
  people?: MentionPeople
  className?: string
}) {
  const segments = parseMentions(body)
  return (
    <p className={cn('whitespace-pre-wrap text-body', className)}>
      {segments.map((segment, index) => {
        // Bare strings, so the text stays the paragraph's own text node.
        if (segment.kind === 'text') return segment.text
        const person = people?.get(segment.userId)
        return (
          <Chip
            key={index}
            tone="accent"
            size="xs"
            className="mx-px align-baseline"
            title={person?.email}
            data-mention-user-id={segment.userId}
          >
            @{person?.name || segment.name}
          </Chip>
        )
      })}
    </p>
  )
}
