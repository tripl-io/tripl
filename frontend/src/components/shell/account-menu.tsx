import { useRef } from 'react'
import { Link } from 'react-router-dom'
import { settingsPath } from '@/lib/activeOrg'
import { LogOut, Palette, Settings, UserCircle } from 'lucide-react'
import {
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
} from '@/components/ui/dropdown-menu'

/**
 * The account menu, shared by the expanded footer and the collapsed rail:
 * Profile, Workspace settings, Appearance, then Sign out behind a separator.
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
          Workspace settings
        </Link>
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
