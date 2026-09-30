/**
 * Leave the SPA for a single sign-on start URL (F20). A full-page navigation,
 * not a router push: the server answers with a redirect to the identity
 * provider, which comes back to the callback and sets the session cookie.
 * Kept in its own module so tests can replace it.
 */
export function navigateToSso(url: string): void {
  window.location.assign(url)
}
