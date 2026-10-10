/**
 * Links from the app to the public documentation site (docs.tripl.io), in one
 * place, so a page that moves there is one change here. Not the in-app docs
 * catalog: its wiki links live in `lib/docLinks.ts`.
 */

/** The documentation home. */
export const DOCS_SITE_URL = 'https://docs.tripl.io'

/** What tripl's words mean: plans, scans, metrics, signals. */
export const CONCEPTS_DOCS_URL = `${DOCS_SITE_URL}/use/concepts`

/** Running tripl on one's own warehouse. */
export const QUICK_START_URL = `${DOCS_SITE_URL}/quick-start`

/** What each edition has: the "Compare editions" link on every Enterprise teaser. */
export const EDITIONS_DOCS_URL = `${DOCS_SITE_URL}/editions`

/** Signing everyone on an instance in through one OpenID Connect provider or
 *  Google, set with environment variables: Community's own single sign-on. */
export const INSTANCE_SIGN_IN_DOCS_URL = `${DOCS_SITE_URL}/administer/admin-guide#instance-sign-in`
