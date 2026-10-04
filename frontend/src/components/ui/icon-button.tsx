import * as React from 'react'
import { type VariantProps } from 'class-variance-authority'
import { Button } from '@/components/ui/button'
import { buttonVariants } from '@/components/ui/button-variants'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'

/**
 * An icon-only button that cannot ship unnamed.
 *
 * `label` is required and does two jobs: it is the accessible name, and it is
 * the visible tooltip on hover AND keyboard focus. A `title=` attribute did
 * neither well — it never appears on focus or touch, and screen readers treat
 * it as a fallback at best — which left keyboard users guessing what a bare
 * trash or X icon would do.
 *
 * It carries its own TooltipProvider so it also works outside the app's root
 * one (tests, portals); an inner provider only sets this tooltip's delay.
 */
export function IconButton({
  label,
  tooltip,
  tooltipSide = 'top',
  variant = 'ghost',
  size = 'icon',
  children,
  ...props
}: Omit<React.ComponentProps<typeof Button>, 'aria-label' | 'title' | 'asChild'> &
  VariantProps<typeof buttonVariants> & {
    /** Accessible name and tooltip text, e.g. "Remove filter". */
    label: string
    /**
     * Longer tooltip text when the button needs more than its name, e.g. what
     * a delete takes with it. Defaults to `label`.
     */
    tooltip?: React.ReactNode
    tooltipSide?: 'top' | 'right' | 'bottom' | 'left'
  }) {
  return (
    <TooltipProvider delayDuration={300}>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button type="button" variant={variant} size={size} aria-label={label} {...props}>
            {children}
          </Button>
        </TooltipTrigger>
        <TooltipContent side={tooltipSide}>{tooltip ?? label}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}
