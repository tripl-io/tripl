import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { authApi } from '@/api/auth'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { PasswordInput } from '@/components/ui/password-input'
import { Label } from '@/components/ui/label'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { getErrorMessage } from '@/lib/utils'
import type { AuthUser } from '@/types'

/**
 * Sign back in over the page the session expired on.
 *
 * A 401 used to drop the user and redirect to /auth, which unmounted whatever
 * form was open: a metric's SQL or an alert rule typed out over ten minutes was
 * gone by the time the user came back. This keeps the page mounted and asks for
 * the password of the same account; the draft is still there to save again.
 *
 * Not dismissable: without a session every request keeps failing, so the only
 * ways out are signing in or signing out.
 */
export function SessionExpiredDialog({
  user,
  onSignedIn,
  onSignOut,
}: {
  user: AuthUser
  onSignedIn: (user: AuthUser) => void
  onSignOut: () => void
}) {
  const [password, setPassword] = useState('')
  const loginMutation = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () => authApi.login({ email: user.email, password }),
    onSuccess: onSignedIn,
  })

  return (
    <Dialog open>
      <DialogContent
        showCloseButton={false}
        className="sm:max-w-sm"
        onEscapeKeyDown={(event) => event.preventDefault()}
        onInteractOutside={(event) => event.preventDefault()}
      >
        <DialogHeader>
          <DialogTitle>Your session has expired</DialogTitle>
          <DialogDescription>
            Sign in again as <strong>{user.email}</strong> to carry on. Anything you have not
            saved is still on the page.
          </DialogDescription>
        </DialogHeader>
        <form
          className="space-y-3"
          // No browser bubble: Sign in stays disabled until there is a
          // password, so there is nothing for native validation to add.
          noValidate
          onSubmit={(event) => {
            event.preventDefault()
            loginMutation.mutate()
          }}
        >
          <div className="space-y-1.5">
            <div className="flex items-baseline justify-between gap-2">
              <Label htmlFor="session-expired-password">Password</Label>
              {/* A new tab, so the draft this dialog protects stays on this
                  page while the reset mail goes out (#237). Signing out
                  was the only other way past a forgotten password. */}
              <a
                href="/auth?mode=forgot"
                target="_blank"
                rel="noopener noreferrer"
                className="text-caption font-medium text-fg-secondary underline-offset-2 hover:underline"
              >
                Forgot password?
              </a>
            </div>
            {/* The same show/hide toggle as the sign-in page (#250). */}
            <PasswordInput
              id="session-expired-password"
              autoComplete="current-password"
              aria-required
              // eslint-disable-next-line jsx-a11y/no-autofocus -- the dialog exists to take this one field
              autoFocus
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </div>
          {loginMutation.isError && (
            <p role="alert" className="text-body text-destructive">
              {getErrorMessage(loginMutation.error)}
            </p>
          )}
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onSignOut}>
              Sign out
            </Button>
            {/* Stays visibly the primary action while it waits for a password:
                the default disabled look is the pale ghost that read as "no
                action here". */}
            <Button
              type="submit"
              disabled={loginMutation.isPending || !password}
              className="disabled:border-transparent disabled:bg-accent-solid disabled:text-accent-solid-fg disabled:opacity-60"
            >
              {loginMutation.isPending ? 'Signing in…' : 'Sign in'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
