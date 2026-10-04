import { countOf } from '@/lib/plural'

/**
 * What a delete actually destroys, in numbers.
 *
 * `AlertDelivery.destination_id` and `.rule_id` are both ON DELETE CASCADE and
 * the inbox INNER JOINs through the deliveries, so removing one rule takes every
 * message it ever sent AND every incident group assembled from them — with the
 * mutes and the notes an operator typed on those incidents. The confirm used to
 * read `Delete "TG"?` and named none of it; on production that single button
 * would have destroyed 115 deliveries and 57 incidents, irreversibly and with no
 * export.
 *
 * It lives in its own module because the rule confirm (DestinationCard), the
 * destination control (DestinationsSection) and the destination confirm on the
 * page all describe the SAME cascade — three copies is how they would start
 * disagreeing about it.
 */
export function describeDeletionImpact(deliveries: number, incidents: number): string {
  if (deliveries === 0 && incidents === 0) {
    return 'It has never delivered, so no history is lost.'
  }
  return (
    `This also deletes ${countOf(deliveries, 'delivery', 'deliveries')} and `
    + `${countOf(incidents, 'incident', 'incidents')} built from them, including any `
    + 'notes and mutes on those incidents. It cannot be undone.'
  )
}
