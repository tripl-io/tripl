import { useMutation, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { chartAnnotationsApi } from '@/api/chartAnnotations'
import type { ConfirmOptions } from '@/hooks/useConfirm'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { projectChartAnnotationsKey } from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import type { ChartAnnotation } from '@/types'

/**
 * Every annotation list of a project — each chart's and the Annotations
 * page's — sits under one key root, and a project-wide marker is in all of
 * them. A change refreshes the root: a chart card that refreshed only its own
 * list left a deleted project-wide marker on every other chart.
 */
export function invalidateAnnotationLists(qc: QueryClient, slug: string | undefined): void {
  void qc.invalidateQueries({ queryKey: projectChartAnnotationsKey(slug) })
}

/**
 * The confirm before deleting an annotation, the same from a chart's card and
 * from the Annotations page. A project-wide marker is drawn on every chart, so
 * deleting it from one removes it from all of them, and the confirm says so.
 */
export function annotationDeleteConfirm(
  annotation: Pick<ChartAnnotation, 'label' | 'scope_type'>,
): ConfirmOptions {
  const projectWide = annotation.scope_type === null
  return {
    title: projectWide ? 'Delete project-wide annotation?' : 'Delete annotation?',
    message: projectWide
      ? `"${annotation.label}" is shown on every chart in this project. Deleting it removes it everywhere.`
      : `"${annotation.label}" will be removed from its chart.`,
    variant: 'danger',
    confirmLabel: 'Delete',
  }
}

/** Deletes an annotation by id and refreshes every annotation list. */
export function useAnnotationDelete(slug: string | undefined) {
  const qc = useQueryClient()
  return useMutation({
    // Its failure is toasted below, with what failed named.
    meta: SILENT_ERROR_META,
    mutationFn: (id: string) => chartAnnotationsApi.delete(slug!, id),
    onSuccess: () => invalidateAnnotationLists(qc, slug),
    onError: error => toast.error(`Could not delete the annotation — ${getErrorMessage(error)}`),
  })
}
