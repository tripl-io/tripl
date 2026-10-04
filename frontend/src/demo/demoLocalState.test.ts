// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { forgetDemoLocalState, sweepOrphanedDemoLocalState } from './demoLocalState'

afterEach(() => {
  window.localStorage.clear()
  window.sessionStorage.clear()
})

describe('demo local state', () => {
  it('forgets one project', () => {
    window.localStorage.setItem('tripl-tour:demo-a', '1')
    window.localStorage.setItem('tripl-tour:demo-b', '1')
    window.sessionStorage.setItem('tripl-demo-hints-muted:demo-a', '1')
    window.sessionStorage.setItem('tripl-tour-dock:demo-a', '3')

    forgetDemoLocalState('demo-a')
    expect(window.sessionStorage.getItem('tripl-tour-dock:demo-a')).toBeNull()

    expect(window.localStorage.getItem('tripl-tour:demo-a')).toBeNull()
    expect(window.sessionStorage.getItem('tripl-demo-hints-muted:demo-a')).toBeNull()
    expect(window.localStorage.getItem('tripl-tour:demo-b')).toBe('1')
  })

  it('sweeps the keys of projects that are gone, and nothing else', () => {
    window.localStorage.setItem('tripl-tour:gone', '1')
    window.localStorage.setItem('tripl-demo-scenario:gone', '{}')
    window.localStorage.setItem('tripl-demo-welcome-dismissed:gone', '1')
    window.localStorage.setItem('tripl-tour:kept', '1')
    window.localStorage.setItem('unrelated-key', 'x')
    window.sessionStorage.setItem('tripl-demo-hints-muted:gone', '1')
    window.sessionStorage.setItem('tripl-tour-dock:gone', '3')
    window.sessionStorage.setItem('tripl-tour-dock:kept', '3')

    sweepOrphanedDemoLocalState(['kept'])
    expect(window.sessionStorage.getItem('tripl-tour-dock:gone')).toBeNull()
    expect(window.sessionStorage.getItem('tripl-tour-dock:kept')).toBe('3')

    expect(window.localStorage.getItem('tripl-tour:gone')).toBeNull()
    expect(window.localStorage.getItem('tripl-demo-scenario:gone')).toBeNull()
    expect(window.localStorage.getItem('tripl-demo-welcome-dismissed:gone')).toBeNull()
    expect(window.sessionStorage.getItem('tripl-demo-hints-muted:gone')).toBeNull()
    expect(window.localStorage.getItem('tripl-tour:kept')).toBe('1')
    expect(window.localStorage.getItem('unrelated-key')).toBe('x')
  })
})
