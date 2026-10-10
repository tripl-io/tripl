import { api } from './client'

/**
 * Every row of a list route that pages with `limit`/`offset` and answers a bare
 * array, read `pageSize` rows at a time until a page comes back short.
 *
 * For a list the app needs whole, such as the member roster behind every name
 * and member picker. One plain request stops at the route's default page, and
 * a bare array carries no total to notice the cut by, so the 201st member of
 * an organization used to be missing from all of them without a word.
 *
 * `pageSize` is at most the route's own `limit` ceiling (a larger one is a
 * 422, not a silent cut), and the route must order its rows stably, or rows
 * shift between pages.
 */
export async function getAllPages<T>(path: string, pageSize: number): Promise<T[]> {
  const separator = path.includes('?') ? '&' : '?'
  let rows: T[] = []
  let page: T[]
  do {
    page = await api.get<T[]>(`${path}${separator}limit=${pageSize}&offset=${rows.length}`)
    rows = rows.concat(page)
  } while (page.length === pageSize)
  return rows
}
