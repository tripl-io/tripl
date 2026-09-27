import { defaultUrlTransform } from 'react-markdown'
import { LINK_HREF_PREFIX } from '@/lib/docLinks'

/**
 * react-markdown's URL filter, letting `tripl:` hrefs (the `[[kind:name]]`
 * doc links) through. Every other URL still goes through the default, which
 * blanks `javascript:`, `data:` and other unsafe protocols.
 */
export function docUrlTransform(url: string): string {
  return url.startsWith(LINK_HREF_PREFIX) ? url : defaultUrlTransform(url)
}
