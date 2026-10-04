import { useCallback } from 'react'
import { useQuery } from '@tanstack/react-query'

import { eventsApi } from '@/api/events'
import { eventTypesApi } from '@/api/eventTypes'
import { metaFieldsApi } from '@/api/metaFields'
import { variablesApi } from '@/api/variables'
import {
  eventsInReviewCountKey,
  eventTagsKey,
  eventTypesKey,
  metaFieldsKey,
  variablesKey,
} from '@/lib/queryKeys'

import {
  EMPTY_EVENT_TYPES,
  EMPTY_META_FIELDS,
  EMPTY_TAGS,
  EMPTY_VARIABLES,
} from './utils'

export function useEventsPageData({
  slug,
  branchId,
}: {
  slug: string | undefined
  branchId: string | null
}) {
  const eventTypesQuery = useQuery({
    queryKey: eventTypesKey(slug, branchId),
    queryFn: () => eventTypesApi.list(slug!, branchId),
    enabled: !!slug,
  })
  const metaFieldsQuery = useQuery({
    queryKey: metaFieldsKey(slug, branchId),
    queryFn: () => metaFieldsApi.list(slug!, branchId),
    enabled: !!slug,
  })
  const variablesQuery = useQuery({
    queryKey: variablesKey(slug, branchId),
    queryFn: () => variablesApi.list(slug!, branchId),
    enabled: !!slug,
  })
  const allTagsQuery = useQuery({
    queryKey: eventTagsKey(slug, branchId),
    queryFn: () => eventsApi.tags(slug!, branchId),
    enabled: !!slug,
  })
  // Counts events whose STATUS is `in_review`, which is what the header stat
  // reports. It is NOT the count of unreviewed events — the `reviewed` flag is
  // an independent axis — and used to be named as if it were.
  const inReviewCountQuery = useQuery({
    queryKey: eventsInReviewCountKey(slug, branchId),
    queryFn: () => eventsApi.list(slug!, { status: ['in_review'], limit: 1 }, branchId),
    enabled: !!slug,
  })

  const refetchPageData = useCallback((): Promise<unknown>[] => [
    eventTypesQuery.refetch(),
    metaFieldsQuery.refetch(),
    variablesQuery.refetch(),
    allTagsQuery.refetch(),
    inReviewCountQuery.refetch(),
  ], [
    allTagsQuery,
    eventTypesQuery,
    metaFieldsQuery,
    inReviewCountQuery,
    variablesQuery,
  ])

  return {
    eventTypes: eventTypesQuery.data ?? EMPTY_EVENT_TYPES,
    /** The type list has arrived, so a type tab that matches none is unknown. */
    eventTypesLoaded: eventTypesQuery.isSuccess,
    metaFields: metaFieldsQuery.data ?? EMPTY_META_FIELDS,
    variables: variablesQuery.data ?? EMPTY_VARIABLES,
    allTags: allTagsQuery.data ?? EMPTY_TAGS,
    inReviewCount: inReviewCountQuery.data?.total ?? 0,
    // The header shows a skeleton, not a 0, until the count has arrived.
    inReviewCountPending: inReviewCountQuery.isPending,
    dataError:
      eventTypesQuery.error ??
      metaFieldsQuery.error ??
      variablesQuery.error ??
      allTagsQuery.error,
    refetchPageData,
  }
}
