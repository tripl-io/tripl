import { describe, expect, it, vi } from 'vitest'
import { isInDemoGuide, keepOpenForDemoGuide } from './demo-guide-layer'

/** An outside-interaction event from a press on `target`, as Radix sends it. */
function pressOn(target: Element): Event {
  const event = new CustomEvent('dismissableLayer.pointerDownOutside', { cancelable: true })
  target.dispatchEvent(event)
  return event
}

describe('demo-guide-layer', () => {
  function guideButton() {
    const guide = document.createElement('div')
    guide.setAttribute('data-demo-guide', '')
    const button = document.createElement('button')
    guide.appendChild(button)
    document.body.appendChild(guide)
    return button
  }

  it('keeps a layer open for a press on the guide, and passes it nothing', () => {
    const handler = vi.fn()
    const button = guideButton()
    button.addEventListener('dismissableLayer.pointerDownOutside', keepOpenForDemoGuide(handler))

    const event = pressOn(button)

    expect(isInDemoGuide(button)).toBe(true)
    expect(event.defaultPrevented).toBe(true)
    expect(handler).not.toHaveBeenCalled()
  })

  it("leaves any other press to the layer's own handler", () => {
    const handler = vi.fn()
    const elsewhere = document.createElement('button')
    document.body.appendChild(elsewhere)
    elsewhere.addEventListener('dismissableLayer.pointerDownOutside', keepOpenForDemoGuide(handler))

    const event = pressOn(elsewhere)

    expect(isInDemoGuide(elsewhere)).toBe(false)
    expect(event.defaultPrevented).toBe(false)
    expect(handler).toHaveBeenCalledWith(event)
  })
})
