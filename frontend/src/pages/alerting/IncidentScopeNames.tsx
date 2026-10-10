import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'

import { INBOX_SCOPE_NAME_LIMIT, incidentMoreScopesLabel } from './inboxCardLabels'

/**
 * "and 3 more" beside an incident's headline, opening the scopes the card
 * carries.
 *
 * It was a "+3 more" with the names in a `title`: nothing on a phone, nothing
 * to a keyboard, so a card covering four scopes named one and hid the rest. The
 * list keeps the server's order — the headline scope first, then the others by
 * the size of their move.
 */
export function IncidentScopeNames({
  names,
  headline,
}: {
  names: readonly string[]
  headline: { more: number; capped: boolean }
}) {
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="whitespace-nowrap text-body-sm font-normal text-fg-tertiary underline decoration-dotted underline-offset-2 hover:text-foreground"
        >
          {incidentMoreScopesLabel(headline)}
          {/* The space sits outside the span: accessible-name computation trims
              each element's text, so a space inside it would read "morescopes". */}
          {' '}
          <span className="sr-only">scopes</span>
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-auto max-w-80 p-3">
        <p className="text-caption font-medium text-fg-tertiary">Scopes in this incident</p>
        <ul className="mt-1.5 flex flex-col gap-1 text-body-sm">
          {names.map(name => (
            <li key={name} className="break-words">
              {name || 'Unnamed scope'}
            </li>
          ))}
        </ul>
        {headline.capped && (
          <p className="mt-2 text-caption text-fg-tertiary">
            {`A card lists up to ${INBOX_SCOPE_NAME_LIMIT} scopes. Search scopes to find any others.`}
          </p>
        )}
      </PopoverContent>
    </Popover>
  )
}
