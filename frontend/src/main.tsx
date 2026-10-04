import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { RouterProvider, createBrowserRouter } from 'react-router-dom'
import {
  MutationCache,
  QueryCache,
  QueryClient,
  QueryClientProvider,
} from '@tanstack/react-query'
// Self-hosted UI fonts: the weights the design uses, bundled
// onto this origin instead of fetched from Google Fonts.
import '@fontsource/inter/400.css'
import '@fontsource/inter/500.css'
import '@fontsource/inter/600.css'
import '@fontsource/inter/700.css'
import '@fontsource/jetbrains-mono/400.css'
import '@fontsource/jetbrains-mono/500.css'
import './index.css'
import App from './App.tsx'
import { ErrorBoundary } from './components/error-boundary.tsx'
import { TooltipProvider } from './components/ui/tooltip.tsx'
import { surfaceMutationError, surfaceQueryError } from './lib/errorFeedback.ts'
import { shouldRetryQuery } from './lib/queryRetry.ts'
import { migrateLegacyOrgStorage } from './lib/activeOrg.ts'

// Once per browser: per-project state saved before organizations existed moves
// under the default organization, where those projects live (F20 PR7).
migrateLegacyOrgStorage()

const queryClient = new QueryClient({
  // Backstop so a failure nobody renders still surfaces a message. A query or
  // mutation that shows its own error opts out with `meta: SILENT_ERROR_META`;
  // see lib/errorFeedback.ts for the rest of the policy.
  queryCache: new QueryCache({ onError: surfaceQueryError }),
  mutationCache: new MutationCache({ onError: surfaceMutationError }),
  defaultOptions: {
    queries: {
      retry: shouldRetryQuery,
      refetchOnWindowFocus: false,
      // Default cache policy: data is fresh for 60s, GC'd 5 min after unmount.
      // Per-query overrides: longer for event types/meta fields (rarely change),
      // shorter (or refetchInterval) for live metrics and monitoring signals.
      staleTime: 60_000,
      gcTime: 5 * 60_000,
    },
  },
})

/**
 * A DATA router, so `useBlocker` exists — and one catch-all route, so nothing
 * else has to change.
 *
 * The settings takeover guards unsaved drafts on every in-app exit and on
 * reload, but could not guard the browser Back button: a blocker is the only
 * thing that sees a navigation BEFORE it commits, and a plain `BrowserRouter`
 * offers none. The alternative — park a spare history entry and read popstate —
 * was built and pulled, because a settings move the draft survives buries the
 * parked entry and every repair for that opened another hole.
 *
 * The route table stays in `App.tsx` exactly as it is. `RouterProvider` puts a
 * data-router context above the whole tree, and a descendant `<Routes>`
 * navigates through `router.navigate` all the same, so every navigation in the
 * app is blocker-visible while all 63 route elements keep their current shape.
 * Migrating them to `createRoutesFromElements` would move the provider stack
 * into a layout route and rewrite every test that mounts the app, and buy
 * nothing this needs.
 *
 * `QueryClientProvider` stays OUTSIDE: no route uses a loader, so nothing in the
 * router asks for it before render.
 */
const router = createBrowserRouter([{ path: '*', element: <App /> }])

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        {/* One provider for the whole app, so `ui/tooltip` works on any page
            instead of throwing outside the two Events screens that mounted
            their own — which is why `title=` stood in for it. Radix
            tooltip already ships in the first-load chunk with the sidebar. */}
        <TooltipProvider delayDuration={300}>
          <RouterProvider router={router} />
        </TooltipProvider>
      </QueryClientProvider>
    </ErrorBoundary>
  </StrictMode>,
)
