import type { MetaFieldDefinition } from '@/types'

export const META_FIELD_LINK_PLACEHOLDER = '${value}'

/**
 * The field types that can hold several values on one event.
 *
 * Mirrors `MULTI_VALUE_FIELD_TYPES` in `schemas/meta_field.py`: `boolean` and
 * `date` are excluded because a second value there is a contradiction, not a
 * list. The server rejects the pair with a 422 — this constant only keeps the
 * settings form from offering a checkbox that cannot be saved.
 */
export const MULTI_VALUE_META_FIELD_TYPES: ReadonlySet<string> = new Set([
  'string',
  'url',
  'enum',
])

/** The example key the form substitutes into a template to show what a value looks like. */
export const META_FIELD_LINK_EXAMPLE_KEY = 'PROJ-1234'

const ABSOLUTE_URL = /^https?:\/\//i

/**
 * The fixed text a template puts around `${value}`, or null when it has no
 * placeholder — same split as the backend's `strip_link_template`
 * (`services/event_service.py`), on the FIRST placeholder.
 */
function templateAround(template: string): { prefix: string; suffix: string } | null {
  const at = template.indexOf(META_FIELD_LINK_PLACEHOLDER)
  if (at === -1) return null
  return {
    prefix: template.slice(0, at),
    suffix: template.slice(at + META_FIELD_LINK_PLACEHOLDER.length),
  }
}

/**
 * The bare key when `value` is exactly `template` applied to one, else `value`.
 *
 * People paste the whole address out of the browser into a field whose template
 * already IS that address around a key: on production every branch-authored
 * event held `https://jira…/browse/PROJ-4770` where `PROJ-4770` was meant, and the
 * rendered link was the template applied to a URL. The server
 * strips on write with this same rule; doing it here too means the form shows
 * what will be stored, not what will be corrected. A template with no fixed
 * prefix wraps nothing, so it strips nothing.
 */
export function stripLinkTemplate(template: string | null | undefined, value: string): string {
  const around = template ? templateAround(template) : null
  if (!around || !around.prefix) return value
  const { prefix, suffix } = around
  const text = value.trim()
  if (
    text.startsWith(prefix)
    && text.endsWith(suffix)
    && text.length > prefix.length + suffix.length
  ) {
    return text.slice(prefix.length, text.length - suffix.length)
  }
  return value
}

/**
 * The link a template would render for an example key, or null when the
 * template has no `${value}` to put it in.
 */
export function metaFieldLinkExample(template: string | null | undefined): string | null {
  if (!template || !template.includes(META_FIELD_LINK_PLACEHOLDER)) return null
  return template.replaceAll(META_FIELD_LINK_PLACEHOLDER, META_FIELD_LINK_EXAMPLE_KEY)
}

/** Schemes a raw meta value may link to. `data:`, `blob:` and the like are text. */
const SAFE_LINK = /^(https?:\/\/|mailto:)/i

/**
 * The scheme an href opens with, lower-cased, or null for a relative one.
 * Browsers drop ASCII tabs and newlines anywhere in a URL and leading
 * whitespace/control characters before parsing, so `java\tscript:` is still
 * `javascript:`; strip them the same way before reading the scheme.
 */
function hrefScheme(href: string): string | null {
  // eslint-disable-next-line no-control-regex
  const normalized = href.replace(/[\t\n\r]/g, '').replace(/^[\u0000-\u0020]+/, '')
  const match = /^([a-z][a-z0-9+.-]*):/i.exec(normalized)
  return match?.[1]?.toLowerCase() ?? null
}

/**
 * Whether a link built from an admin's template may be an anchor. The backend
 * accepts any template holding the placeholder (`schemas/meta_field.py`), so
 * relative links such as `/wiki/${value}` are legitimate; only a scheme other
 * than http(s) or mailto — `javascript:`, `data:`, `vbscript:` — is refused.
 */
function isSafeTemplateLink(href: string): boolean {
  const scheme = hrefScheme(href)
  return scheme === null || scheme === 'http' || scheme === 'https' || scheme === 'mailto'
}

/**
 * A key as it goes into a link template: URL-encoded, so a key with a space,
 * `#`, `?` or `&` cannot end the path early or start a fragment. `/` stays as
 * it is: templates such as `https://github.com/${value}` take `org/repo` keys
 * whose slashes are meant as path separators.
 */
function encodeTemplateValue(value: string): string {
  return encodeURIComponent(value).replaceAll('%2F', '/')
}

export function resolveMetaFieldHref(
  metaField: Pick<MetaFieldDefinition, 'field_type' | 'link_template'>,
  value: string,
) {
  if (!value) {
    return null
  }
  // A stored value is user input: used as the href itself, only web and mail
  // links become anchors, so a `data:` or other scheme renders as plain text.
  const rawLink = (href: string) => (SAFE_LINK.test(href) ? href : null)
  if (metaField.link_template) {
    // A value that already is a link — pasted whole, or stored before the
    // server began stripping — must not be wrapped in the template a second
    // time.
    if (ABSOLUTE_URL.test(value)) return rawLink(value)
    // The value IS the template around a key, so it is the link the template
    // would build and is judged as one (a relative template stays a link).
    if (stripLinkTemplate(metaField.link_template, value) !== value) {
      return isSafeTemplateLink(value) ? value : null
    }
    const href = metaField.link_template.replaceAll(
      META_FIELD_LINK_PLACEHOLDER,
      encodeTemplateValue(value),
    )
    return isSafeTemplateLink(href) ? href : null
  }
  if (metaField.field_type === 'url') return rawLink(value.trim())
  return null
}
