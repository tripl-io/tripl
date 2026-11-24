export function mappingError(value: string) {
  if (!value) return null
  if (new TextEncoder().encode(value).length > 2000) return 'Mapping exceeds 2000 bytes.'
  try {
    const parsed: unknown = JSON.parse(value)
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) return null
  } catch { /* Share the same validation message for malformed and scalar JSON. */ }
  return 'Enter a JSON object.'
}
