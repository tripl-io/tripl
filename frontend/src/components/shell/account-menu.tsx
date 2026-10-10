import { useRef } from 'react'
import { Link } from 'react-router-dom'
import { settingsPath } from '@/lib/activeOrg'
import { DOCS_SITE_URL } from '@/lib/docsSite'
import { BookOpen, ExternalLink, LogOut, Palette, Settings, UserCircle } from 'lucide-react'
import {
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
} from '@/components/ui/dropdown-menu'

/**
 * The account menu, shared by the expanded footer and the collapsed rail:
 * Profile, Settings, Documentation, Appearance, then Sign out behind a
 * separator. "Settings", not "Workspace settings": the settings rail has no
 * Workspace group (it reads Project, Organization, Account), and the sidebar's
 * own link to the same page says Settings.
 */
export function AccountMenuContent({
  side,
  align,
  userLabel,
  isLoggingOut,
  onSignOut,
  onOpenTweaks,
}: {
  side: 'top' | 'right'
  align: 'start' | 'end'
  userLabel: string
  isLoggingOut: boolean
  onSignOut: () => void
  onOpenTweaks: () => void
}) {
  // Picking Appearance opens a popover that takes focus; the menu must not
  // then pull focus back to its own trigger as it closes.
  const openingTweaksRef = useRef(false)
  return (
    <DropdownMenuContent
      side={side}
      align={align}
      sideOffset={8}
      className="w-[220px]"
      onCloseAutoFocus={(event) => {
        if (!openingTweaksRef.current) return
        openingTweaksRef.current = false
        event.preventDefault()
      }}
    >
      <DropdownMenuLabel className="truncate text-body-sm">{userLabel}</DropdownMenuLabel>
      <DropdownMenuSeparator />
      <DropdownMenuItem asChild>
        <Link to="/settings/profile" className="flex items-center gap-2 text-body-sm no-underline">
          <UserCircle className="size-4" aria-hidden="true" />
          Profile
        </Link>
      </DropdownMenuItem>
      <DropdownMenuItem asChild>
        <Link to={settingsPath('/settings')} className="flex items-center gap-2 text-body-sm no-underline">
          <Settings className="size-4" aria-hidden="true" />
          Settings
        </Link>
      </DropdownMenuItem>
      <DropdownMenuItem asChild>
        <a
          href={DOCS_SITE_URL}
          target="_blank"
          rel="noreferrer"
          aria-label="Documentation (opens in a new tab)"
          className="flex items-center gap-2 text-body-sm no-underline"
        >
          <BookOpen className="size-4" aria-hidden="true" />
          <span className="flex-1">Documentation</span>
          <ExternalLink className="size-3.5 text-fg-subtle" aria-hidden="true" />
        </a>
      </DropdownMenuItem>
      <DropdownMenuItem
        onSelect={() => {
          openingTweaksRef.current = true
          onOpenTweaks()
        }}
        className="flex items-center gap-2 text-body-sm">
        <Palette className="size-4" aria-hidden="true" />
        Appearance
      </DropdownMenuItem>
      <DropdownMenuSeparator />
      <DropdownMenuItem
        onSelect={onSignOut}
        disabled={isLoggingOut}
        className="flex items-center gap-2 text-body-sm"
      >
        <LogOut className="size-4" aria-hidden="true" />
        {isLoggingOut ? 'Signing out…' : 'Sign out'}
      </DropdownMenuItem>
    </DropdownMenuContent>
  )
}
