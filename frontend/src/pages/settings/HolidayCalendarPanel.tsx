import { useId } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { anomalySettingsApi } from '@/api/anomalySettings'
import { NativeSelect, Panel } from '@/components/settings/kit'
import { ReadOnlyDefinition } from '@/components/states'
import { Label } from '@/components/ui/label'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { holidayCountriesKey, projectAnomalySettingsKey } from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import { invalidatePlannedEventEffects } from '@/pages/monitoring/plannedEventMutations'

const regionNames = new Intl.DisplayNames(['en'], { type: 'region' })

/** "Germany (DE)"; the bare code where the browser has no name for it. */
function countryLabel(code: string): string {
  let name: string | undefined
  try {
    name = regionNames.of(code)
  } catch {
    name = undefined
  }
  return name && name !== code ? `${name} (${code})` : code
}

const SUBTITLE =
  "A country's public holidays become project-wide expected windows: a holiday's dip or surge is still drawn, but never becomes a signal or an alert."

/**
 * The project's holiday calendar (F18): a country whose public holidays become
 * project-wide expected windows. The backend writes those rows when the country
 * changes and rolls them into each new year nightly; here it is one select.
 */
export function HolidayCalendarPanel({
  slug,
  country,
  canWrite,
}: {
  slug: string
  country: string | null
  canWrite: boolean
}) {
  const qc = useQueryClient()
  const selectId = useId()
  const countriesQuery = useQuery({
    queryKey: holidayCountriesKey(slug),
    queryFn: () => anomalySettingsApi.holidayCountries(slug),
    staleTime: Infinity,
    enabled: canWrite,
    // Rendered under the select.
    meta: SILENT_ERROR_META,
  })

  const updateMut = useMutation({
    // Its error is rendered under the select.
    meta: SILENT_ERROR_META,
    mutationFn: (code: string | null) => anomalySettingsApi.update(slug, { holiday_country: code }),
    onSuccess: (_data, code) => {
      void qc.invalidateQueries({ queryKey: projectAnomalySettingsKey(slug) })
      // Adding or removing the holiday windows retags anomalies like any other
      // window write: the series, the signals and the sidebar badge follow.
      invalidatePlannedEventEffects(qc, slug)
      toast.success(
        code ? `Holidays of ${countryLabel(code)} added as expected windows` : 'Holiday calendar removed',
      )
    },
  })

  if (!canWrite) {
    return (
      <Panel title="Holiday calendar" subtitle={SUBTITLE}>
        <div className="p-4">
          <ReadOnlyDefinition
            items={[{ label: 'Country', value: country ? countryLabel(country) : 'None' }]}
          />
        </div>
      </Panel>
    )
  }

  const codes = countriesQuery.data ?? (country ? [country] : [])
  const options = [
    { value: '', label: 'None' },
    ...codes
      .map(code => ({ value: code, label: countryLabel(code) }))
      .sort((a, b) => a.label.localeCompare(b.label)),
  ]

  return (
    <Panel title="Holiday calendar" subtitle={SUBTITLE}>
      <div className="grid gap-1.5 p-4">
        <Label htmlFor={selectId}>Country</Label>
        <NativeSelect
          id={selectId}
          value={country ?? ''}
          options={options}
          disabled={updateMut.isPending || countriesQuery.isPending}
          onChange={value => updateMut.mutate(value || null)}
          aria-describedby={`${selectId}-hint`}
        />
        <p id={`${selectId}-hint`} className="m-0 text-caption text-fg-tertiary">
          Each holiday is one UTC day, for last year, this year and next. They are listed on the
          Annotations page and cannot be edited one by one; pick None to remove them.
        </p>
        {countriesQuery.isError && (
          <p role="alert" className="m-0 text-body-sm text-destructive">
            Couldn&apos;t load the country list: {getErrorMessage(countriesQuery.error)}
          </p>
        )}
        {updateMut.isError && (
          <p role="alert" className="m-0 text-body-sm text-destructive">
            {getErrorMessage(updateMut.error)}
          </p>
        )}
      </div>
    </Panel>
  )
}
