import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from './tooltip'

describe('TooltipContent', () => {
  it('is an inverse neutral chip, not a brand-coloured one', () => {
    render(
      <TooltipProvider>
        <Tooltip open>
          <TooltipTrigger>Help</TooltipTrigger>
          <TooltipContent>Explains the control</TooltipContent>
        </Tooltip>
      </TooltipProvider>,
    )

    const content = document.querySelector('[data-slot="tooltip-content"]')
    expect(content).not.toBeNull()
    expect(content).toHaveClass('bg-foreground', 'text-background', 'text-caption')
    expect(content).not.toHaveClass('bg-primary')
    expect(content).not.toHaveClass('text-xs')
  })
})
