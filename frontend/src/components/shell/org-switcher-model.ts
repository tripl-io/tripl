/** Where the takeover's organization page lives; `?create=1` opens "Create". */
export const ORG_SETTINGS_PATH = '/settings/organization/general'

/**
 * Whether the organization switcher is drawn: only for someone in more than
 * one organization. With one there is nothing to switch to, and a control
 * that names the only organization on every page is noise (owner decision 2).
 */
export function shouldShowOrgSwitcher(orgCount: number): boolean {
  return orgCount > 1
}
