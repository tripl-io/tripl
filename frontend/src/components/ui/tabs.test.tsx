import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Tabs, TabsContent, TabsList, TabsTrigger } from './tabs'

describe('TabsContent focus indicator', () => {
  // Radix makes the panel a Tab stop (tabIndex=0). The global :focus-visible
  // outline lives in @layer base and loses to `outline-none`, so the panel has
  // to bring its own ring or keyboard focus lands on something invisible.
  it('is focusable and carries its own focus-visible ring', () => {
    render(
      <Tabs defaultValue="a">
        <TabsList>
          <TabsTrigger value="a">A</TabsTrigger>
        </TabsList>
        <TabsContent value="a">Panel A</TabsContent>
      </Tabs>,
    )
    const panel = screen.getByRole('tabpanel')
    expect(panel).toHaveAttribute('tabindex', '0')
    expect(panel.className).toMatch(/focus-visible:ring-\[3px\]/)
    expect(panel.className).toMatch(/focus-visible:ring-ring\/50/)
  })
})

describe('TabsList variant="segmented"', () => {
  it('draws the segmented look and keeps tab semantics', () => {
    render(
      <Tabs defaultValue="inbox">
        <TabsList variant="segmented" aria-label="Alerting">
          <TabsTrigger value="inbox" count={2} countUrgent>
            Inbox
          </TabsTrigger>
          <TabsTrigger value="rules">Rules</TabsTrigger>
        </TabsList>
        <TabsContent value="inbox">Inbox panel</TabsContent>
        <TabsContent value="rules">Rules panel</TabsContent>
      </Tabs>,
    )

    const list = screen.getByRole('tablist', { name: 'Alerting' })
    expect(list).toHaveClass('bg-bg-sunken', 'rounded-control')
    expect(list).not.toHaveClass('border-b')

    // The count is part of the tab's name, and the strip is a real tablist.
    const inbox = screen.getByRole('tab', { name: 'Inbox (2)' })
    expect(inbox).toHaveAttribute('aria-selected', 'true')
    expect(inbox.className).toContain('data-[state=active]:bg-surface')
    expect(inbox.className).not.toContain('border-b-2')

    fireEvent.mouseDown(screen.getByRole('tab', { name: 'Rules' }))
    expect(screen.getByRole('tabpanel')).toHaveTextContent('Rules panel')
  })

  it('keeps the underline look by default', () => {
    render(
      <Tabs defaultValue="a">
        <TabsList>
          <TabsTrigger value="a">A</TabsTrigger>
        </TabsList>
      </Tabs>,
    )
    expect(screen.getByRole('tablist')).toHaveClass('border-b')
    expect(screen.getByRole('tab', { name: 'A' }).className).toContain('border-b-2')
  })
})
