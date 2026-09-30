import { Link, useNavigate } from 'react-router-dom'
import { Building2, Check, ChevronsUpDown, Plus, Settings } from 'lucide-react'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { useActiveOrg } from '@/components/active-org-context'
import { orgHomePath, settingsPath } from '@/lib/activeOrg'
import { useCanCreateOrg } from '@/lib/deploymentMode'
import { cn } from '@/lib/utils'
import { ORG_SETTINGS_PATH, shouldShowOrgSwitcher } from './org-switcher-model'
import { ICON_BUTTON_CLASS } from './sidebar-style'

/**
 * The organization switcher (F20 PR7), above the project switcher: projects
 * belong to an organization, so the organization is picked first. Picking one
 * opens its workspace, `/o/{org}` — a project page of the old organization has
 * no meaning in the new one.
 */
export function OrgSwitcher({ compact = false }: { compact?: boolean }) {
  const { slug, membership, orgs } = useActiveOrg()
  const navigate = useNavigate()
  if (!shouldShowOrgSwitcher(orgs.length)) return null

  const name = membership?.name ?? slug ?? 'Organization'

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        {compact ? (
          <button
            type="button"
            aria-label={`Switch organization (current: ${name})`}
            title={name}
            className={cn(ICON_BUTTON_CLASS, 'mb-1')}
          >
            <Building2 className="size-4" aria-hidden="true" />
          </button>
        ) : (
          <button
            type="button"
            aria-label={`Switch organization (current: ${name})`}
            className="flex w-full items-center gap-2 rounded-md px-2 py-1 text-left transition-colors hover:bg-sidebar-hover"
          >
            <Building2 className="size-3.5 shrink-0 text-fg-tertiary" aria-hidden="true" />
            <span className="min-w-0 flex-1 truncate text-body-sm font-medium text-fg-secondary">{name}</span>
            <ChevronsUpDown className="size-3 shrink-0 text-fg-tertiary" aria-hidden="true" />
          </button>
        )}
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        side={compact ? 'right' : 'bottom'}
        sideOffset={6}
        className="w-[260px]"
      >
        <DropdownMenuLabel className="micro-label text-fg-tertiary">Organizations</DropdownMenuLabel>
        <div className="max-h-[320px] overflow-y-auto">
          {orgs.map((org) => (
            <DropdownMenuItem
              key={org.slug}
              onSelect={() => navigate(orgHomePath(org.slug))}
              // The check mark is decoration; this and the hidden text below
              // are what a screen reader announces for the current one.
              aria-current={org.slug === slug ? 'true' : undefined}
              className="flex items-center gap-2 text-body-sm"
            >
              <div className="min-w-0 flex-1">
                <div className="truncate">
                  {org.name}
                  {org.slug === slug && <span className="sr-only">{', current'}</span>}
                </div>
                <div className="mono truncate text-micro text-fg-tertiary">{org.slug}</div>
              </div>
              {org.slug === slug && <Check className="size-3.5 shrink-0 text-accent" aria-hidden="true" />}
            </DropdownMenuItem>
          ))}
        </div>
        <DropdownMenuSeparator />
        <DropdownMenuItem asChild>
          <Link to={settingsPath(ORG_SETTINGS_PATH)} className="flex items-center gap-2 text-body-sm no-underline text-fg">
            <Settings className="size-3.5 shrink-0 text-fg-tertiary" aria-hidden="true" />
            Organization settings
          </Link>
        </DropdownMenuItem>
        <CreateOrgMenuItem />
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/**
 * "Create organization", for a platform admin or anyone in hosted mode (F20
 * hosted sign-up). Its own component so the instance probe it may need runs
 * when the menu opens, not on every page the switcher sits on.
 */
function CreateOrgMenuItem() {
  const canCreateOrg = useCanCreateOrg()
  if (!canCreateOrg) return null
  return (
    <DropdownMenuItem asChild>
      <Link
        to={settingsPath(`${ORG_SETTINGS_PATH}?create=1`)}
        className="flex items-center gap-2 text-body-sm no-underline text-fg"
      >
        <Plus className="size-3.5 shrink-0 text-fg-tertiary" aria-hidden="true" />
        Create organization
      </Link>
    </DropdownMenuItem>
  )
}
