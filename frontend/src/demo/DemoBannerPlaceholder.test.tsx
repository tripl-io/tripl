import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { DemoBannerPlaceholder } from './DemoBannerPlaceholder'

describe('DemoBannerPlaceholder (#251)', () => {
  it('holds the banner footprint without anything to read or reach', () => {
    render(<DemoBannerPlaceholder />)

    const box = screen.getByTestId('demo-banner-placeholder')
    // The banner's own bottom margin, so the page below does not move.
    expect(box).toHaveClass('mb-4')
    expect(box).toHaveAttribute('aria-hidden', 'true')
    expect(box.querySelector('.lg\\:hidden')).not.toBeNull()
    // The banner's structure: a bordered panel around an h-11 row, so the
    // border adds to the row's 44px exactly as it does on the real bar.
    const bar = box.querySelector('.lg\\:block')
    expect(bar).toHaveClass('border')
    expect(bar).not.toHaveClass('h-11')
    expect(bar?.firstElementChild).toHaveClass('h-11')
    // Marked like the real banner, so the demo guide in a top corner clears it (#251).
    expect(box).toHaveAttribute('data-demo-banner')
    expect(screen.queryByRole('button')).toBeNull()
  })
})
