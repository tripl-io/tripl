import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { usersApi } from '@/api/users'
import { usersKey } from '@/lib/queryKeys'

/**
 * `id -> display name`, plus whether the roster behind it has arrived.
 *
 * Still a `Map`, so every existing caller that reads `.get()` or passes it on
 * keeps working; `status` rides along for `displayUser`, which must not call a
 * person "unknown" merely because `/users` is still loading or failed.
 */
export type UsersById = Map<string, string> & {
  readonly status?: 'pending' | 'error' | 'success'
}

/**
 * The organization's roster as `id -> display name`, for turning a stored
 * `user_id` into a person. `usersApi.list` reads every page of it, so a member
 * past the route's first page still has a name.
 *
 * Resolving client-side is the convention here rather than embedding a name in
 * every response: the id is already on the wire, and one shared `['users']`
 * query serves every caller on the page — react-query dedupes the fetch, so
 * three panels asking for it cost one request.
 *
 * `GET /users` is open to any authenticated user (`api/v1/users.py` says so in
 * as many words), so no caller needs a permission branch around it.
 */
export function useUsersById(): UsersById {
  const { data: users, status } = useQuery({
    queryKey: usersKey(),
    queryFn: () => usersApi.list(),
  })
  return useMemo(
    () =>
      Object.assign(new Map((users ?? []).map((u) => [u.id, u.name ?? u.email] as const)), {
        status,
      }),
    [users, status],
  )
}

/** Shown in place of a name while the roster is still on its way. */
export const USER_PENDING_LABEL = '…'
/** Shown when the roster could not be loaded, so the name is unknowable, not unknown. */
export const USER_UNAVAILABLE_LABEL = 'name unavailable'

/**
 * The name behind a `user_id`.
 *
 * `'unknown'` is reserved for an account that is really gone — a deleted
 * account, whose FK is SET NULL precisely so the row it wrote survives it. It
 * used to be printed for everyone while `/users` loaded, and for good if it
 * failed, so audit-style surfaces misattributed every action to a deleted
 * account.
 */
export function displayUser(usersById: UsersById, userId: string | null | undefined): string {
  if (!userId) return 'unknown'
  const name = usersById.get(userId)
  if (name !== undefined) return name
  if (usersById.status === 'pending') return USER_PENDING_LABEL
  if (usersById.status === 'error') return USER_UNAVAILABLE_LABEL
  return 'unknown'
}
