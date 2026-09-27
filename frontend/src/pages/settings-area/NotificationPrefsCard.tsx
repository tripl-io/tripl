import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { notificationsApi } from '@/api/notifications'
import { Field, RadioCards, SCard, ToggleRow } from '@/components/settings/kit'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { myNotificationPrefsKey } from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import type { NotificationEmailMode, NotificationPrefs, NotificationPrefsUpdate } from '@/types'

const EMAIL_MODE_OPTIONS = [
  { value: 'off', label: 'Off', description: 'In the app only.' },
  { value: 'instant', label: 'Instantly', description: 'One email per notification.' },
  { value: 'daily', label: 'Daily digest', description: 'One email a day with what you have not read.' },
  { value: 'weekly', label: 'Weekly digest', description: 'One email a week with what you have not read.' },
] as const

function isEmailMode(value: string): value is NotificationEmailMode {
  return EMAIL_MODE_OPTIONS.some(option => option.value === value)
}

/**
 * Account · Profile · Notifications (#259): how the reader's own notifications
 * reach them by email. The in-app bell always has them; this only decides
 * whether and how often email carries them too. Each change saves at once.
 */
export function NotificationPrefsCard() {
  const qc = useQueryClient()
  const prefsQuery = useQuery({
    queryKey: myNotificationPrefsKey(),
    queryFn: ({ signal }) => notificationsApi.getPrefs(signal),
    meta: SILENT_ERROR_META,
  })
  const saveMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (patch: NotificationPrefsUpdate) => notificationsApi.updatePrefs(patch),
    onMutate: async patch => {
      await qc.cancelQueries({ queryKey: myNotificationPrefsKey() })
      const previous = qc.getQueryData<NotificationPrefs>(myNotificationPrefsKey())
      if (previous) qc.setQueryData(myNotificationPrefsKey(), { ...previous, ...patch })
      return { previous }
    },
    onError: (error, _patch, context) => {
      if (context?.previous) qc.setQueryData(myNotificationPrefsKey(), context.previous)
      toast.error(`Could not save notification settings — ${getErrorMessage(error)}`)
    },
    onSuccess: saved => qc.setQueryData(myNotificationPrefsKey(), saved),
  })

  const prefs = prefsQuery.data
  return (
    <SCard
      title="Notifications"
      description="Comments, mentions, review requests and signals on what you watch. The bell always shows them; choose how email carries them too."
    >
      {prefsQuery.isError ? (
        <p role="alert" className="px-4 py-[15px] text-body-sm text-destructive">
          Notification settings could not be loaded: {getErrorMessage(prefsQuery.error)}
        </p>
      ) : !prefs ? (
        <p className="px-4 py-[15px] text-body-sm text-fg-tertiary">Loading…</p>
      ) : (
        <>
          {!prefs.email_available && (
            <p className="border-b px-4 py-2.5 text-body-sm text-fg-secondary border-border-subtle">
              This instance has no outgoing email set up yet. Your choice is kept, and emails start once
              an owner configures it.
            </p>
          )}
          <Field label="Email" stacked>
            <RadioCards
              groupLabel="Email frequency"
              value={prefs.email_mode}
              onChange={value => {
                if (isEmailMode(value) && value !== prefs.email_mode) saveMut.mutate({ email_mode: value })
              }}
              options={EMAIL_MODE_OPTIONS}
              columns={2}
              disabled={saveMut.isPending}
            />
          </Field>
          <ToggleRow
            label="Email me when I am mentioned"
            hint="Sent right away, whatever the frequency above."
            value={prefs.mentions_email}
            onChange={value => saveMut.mutate({ mentions_email: value })}
            disabled={saveMut.isPending}
            last
          />
        </>
      )}
    </SCard>
  )
}
