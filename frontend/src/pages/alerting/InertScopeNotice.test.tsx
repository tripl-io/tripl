import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import { InertScopeNotice, inertScopeSentence, type DriftScope } from './InertScopeNotice'

// `slug` is deliberately REQUIRED rather than defaulted: a default would swallow
// an explicit `undefined` and silently re-supply the slug, so the no-project test
// below would pass while never exercising the branch it names.
function renderNotice(
  scope: DriftScope,
  slug: string | undefined,
  newTab?: boolean,
  scanConfigId?: string,
) {
  return render(
    <MemoryRouter>
      <InertScopeNotice slug={slug} scope={scope} newTab={newTab} scanConfigId={scanConfigId} />
    </MemoryRouter>,
  )
}

/**
 * The shared copy table behind both screens that mark an inert scope
 * (tripl-wkwv.1). Asserted here rather than only through its two callers,
 * because the wording is the whole fix: a sentence that named no missing thing,
 * or linked nowhere, would leave the reader exactly where the bug found them.
 */
describe('InertScopeNotice', () => {
  it.each([
    ['distribution_drift', 'Scan settings', '/p/acme-ios/scans'],
    ['variable_value_drift', 'Variables', '/p/acme-ios/variables'],
  ] as const)('sends %s to the screen that supplies its data', (scope, label, href) => {
    renderNotice(scope, 'acme-ios')

    expect(screen.getByText(inertScopeSentence(scope))).toBeInTheDocument()
    expect(screen.getByRole('link', { name: label })).toHaveAttribute('href', href)
  })

  it('points a scan-bound reader at that scan, and says exactly the same thing', () => {
    // The verdict behind this notice is the PROJECT's and stays the project's,
    // so the SENTENCE must not move an inch when a scan is named — only where
    // the link lands. A second wording here would be the monitor detail and the
    // rule editor describing one rule two ways (tripl-wkwv.9, tripl-oxkt.18).
    renderNotice('distribution_drift', 'acme-ios', false, 'scan-42')

    expect(screen.getByText(inertScopeSentence('distribution_drift'))).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Scan settings' })).toHaveAttribute(
      'href',
      '/p/acme-ios/scans/scan-42',
    )
  })

  it('falls back to the scan list when the caller names no scan', () => {
    // The rule editor is that caller: its draft's scan changes under the
    // reader's hand and its readiness block is the project's, so it passes
    // nothing and must keep landing on the list exactly as before.
    renderNotice('distribution_drift', 'acme-ios')

    expect(screen.getByRole('link', { name: 'Scan settings' })).toHaveAttribute(
      'href',
      '/p/acme-ios/scans',
    )
  })

  it('ignores the scan for value drift, which has no scan dimension', () => {
    // Variables carry a project and a branch, never a scan — so there is no
    // per-scan variables screen to send anyone to, and narrowing this link would
    // invent one. The prop is accepted here only because one component serves
    // both scopes.
    renderNotice('variable_value_drift', 'acme-ios', false, 'scan-42')

    expect(screen.getByRole('link', { name: 'Variables' })).toHaveAttribute(
      'href',
      '/p/acme-ios/variables',
    )
  })

  it('names the missing thing without calling the rule broken', () => {
    // "cannot fire until one does" is a precondition, not an accusation: the
    // operator deliberately switched this scope on and did nothing wrong.
    for (const scope of ['distribution_drift', 'variable_value_drift'] as const) {
      const sentence = inertScopeSentence(scope)
      expect(sentence).toMatch(/cannot fire until one does\.$/)
      expect(sentence).not.toMatch(/broken|invalid|error|misconfigur/i)
    }
  })

  it('says value drift is measured on the main branch, not on whatever is open', () => {
    // Detection reads main, so a working branch that documents values changes
    // nothing until it merges. Without this the notice reads as flatly false to
    // anyone looking at such a branch.
    expect(inertScopeSentence('variable_value_drift')).toContain('on the main branch')
  })

  it('carries the exclusion qualifier the readiness probe enforces', () => {
    // The probe skips variables excluded from scans, so a project whose only
    // documented variable is excluded still gets this notice. Without these
    // words the sentence denies a documented list that plainly exists — the
    // verdict is right, the claim behind it was not.
    expect(inertScopeSentence('variable_value_drift')).toContain('that scans observe')
  })

  it('warns that the Variables link opens on the selected branch, not main', () => {
    // The sentence is about main; the link is about whatever branch is
    // selected. Saying so is the honest fix — a help link that reset the
    // app-wide branch selection as a side effect is not.
    renderNotice('variable_value_drift', 'acme-ios')

    expect(screen.getByText(/opens on the branch you have selected/)).toBeInTheDocument()
    // Only this scope has the mismatch: ScanConfig has no branch at all.
    expect(inertScopeSentence('distribution_drift')).not.toContain('branch')
  })

  it('stays in this tab by default, and leaves only when a caller asks', () => {
    // React Router hands `_blank` back to the browser, which is what lets the
    // rule editor's modal survive a click on its own advice.
    const sameTab = renderNotice('distribution_drift', 'acme-ios')
    expect(screen.getByRole('link', { name: 'Scan settings' })).not.toHaveAttribute('target')
    sameTab.unmount()

    renderNotice('distribution_drift', 'acme-ios', true)
    const link = screen.getByRole('link', { name: 'Scan settings' })
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noreferrer')
    // Same destination either way — the tab is the only difference.
    expect(link).toHaveAttribute('href', '/p/acme-ios/scans')
  })

  it('does not announce itself — it is standing state, not an event', () => {
    // Both callers poll, so a live region here would interrupt on every refetch.
    renderNotice('distribution_drift', 'acme-ios')

    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('still states the problem when there is no project to link to', () => {
    // `slug` comes from the route params and is typed optional. Dropping the
    // sentence with the link would lose the only thing worth saying.
    renderNotice('distribution_drift', undefined)

    expect(screen.getByText(inertScopeSentence('distribution_drift'))).toBeInTheDocument()
    expect(screen.queryByRole('link')).toBeNull()
  })
})
