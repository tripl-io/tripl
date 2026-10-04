import { describe, expect, it } from 'vitest'
import { DEFAULT_RANGE_DAYS, readMonitoringDetailSearch } from './useMonitoringDetailSearch'

describe('readMonitoringDetailSearch', () => {
  it('reads every view param a link can carry', () => {
    const search = readMonitoringDetailSearch(new URLSearchParams(
      'tab=breakdowns&range=90&gran=day&version=latest&field=country&column=platform&value=ios&value=web',
    ))
    expect(search).toEqual({
      tab: 'breakdowns',
      rangeDays: 90,
      granularity: 'day',
      versionFilter: 'latest',
      distributionField: 'country',
      breakdownColumn: 'platform',
      breakdownValues: ['ios', 'web'],
    })
  })

  it('degrades anything unknown to its default instead of an empty view', () => {
    const search = readMonitoringDetailSearch(new URLSearchParams('tab=nope&range=13&gran=decade&version=x'))
    expect(search.tab).toBe('volume')
    expect(search.rangeDays).toBe(DEFAULT_RANGE_DAYS)
    expect(search.granularity).toBeNull()
    expect(search.versionFilter).toBe('all')
    expect(search.breakdownValues).toEqual([])
  })
})
