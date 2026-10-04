import { Profiler } from 'react'
import { act, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { createNoteDraftStore, useNoteDraft, type NoteDraftStore } from './noteDraftStore'

/**
 * typing a note re-rendered the whole alerting page and every incident
 * card on it. The drafts now live in a store each card subscribes to by id, so
 * what has to hold is that a keystroke in one card's draft reaches that card
 * and no other.
 */
describe('noteDraftStore', () => {
  it('reads back what was written, per incident', () => {
    const store = createNoteDraftStore({ a: 'seeded' })

    store.set('b', 'typed')

    expect(store.get('a')).toBe('seeded')
    expect(store.get('b')).toBe('typed')
    expect(store.get('c')).toBe('')
  })

  it('clears a saved draft, but not one the reader kept typing into', () => {
    const store = createNoteDraftStore({ a: 'first draft', b: 'first draft, and more' })

    store.clearIfUnchanged('a', 'first draft')
    // Sent "first draft", but the box now holds more than that: the extra words
    // never reached the server and must not be wiped by the success handler.
    store.clearIfUnchanged('b', 'first draft')

    expect(store.get('a')).toBe('')
    expect(store.get('b')).toBe('first draft, and more')
  })

  it('re-renders only the card whose draft changed', () => {
    const store = createNoteDraftStore()
    const renders: Record<string, number> = { a: 0, b: 0 }
    // Counted by a Profiler around each card rather than from inside the card,
    // so the component under test stays a pure render.
    const countRender = (id: string) => {
      renders[id] = (renders[id] ?? 0) + 1
    }

    function Card({ id, source }: { id: string; source: NoteDraftStore }) {
      const draft = useNoteDraft(source, id)
      return <p data-testid={id}>{draft}</p>
    }

    render(
      <>
        <Profiler id="a" onRender={countRender}>
          <Card id="a" source={store} />
        </Profiler>
        <Profiler id="b" onRender={countRender}>
          <Card id="b" source={store} />
        </Profiler>
      </>,
    )
    const before = { ...renders }

    act(() => store.set('a', 'why this matters'))

    expect(screen.getByTestId('a')).toHaveTextContent('why this matters')
    expect(renders.a).toBe((before.a ?? 0) + 1)
    expect(renders.b).toBe(before.b)
  })

  it('does not notify anyone for a write that changes nothing', () => {
    const store = createNoteDraftStore({ a: 'same' })
    let calls = 0
    store.subscribe('a', () => { calls += 1 })

    store.set('a', 'same')

    expect(calls).toBe(0)
  })
})
