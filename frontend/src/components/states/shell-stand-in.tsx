import type { CSSProperties, ReactNode } from 'react'
import { cn } from '@/lib/utils'

/**
 * A full-viewport screen in place of the app shell or a page: a project that
 * could not be opened, an organization the user cannot enter, a signed-in
 * visitor on a link meant for someone without a session. It is the page's
 * main landmark, since no shell renders one around it. `justify-center-safe`:
 * a stand-in that lists projects or organizations can outgrow a phone screen,
 * and plain centring clipped its top.
 */
export function ShellStandIn({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div
      role="main"
      className={cn(
        'flex h-screen flex-col items-center justify-center-safe overflow-y-auto px-6 py-8 supports-[height:100dvh]:h-dvh bg-background',
        className,
      )}
    >
      {children}
    </div>
  )
}

const GATE_CARD_STYLE: CSSProperties = { borderColor: 'var(--border)', background: 'var(--surface)' }

/**
 * The card a stop in front of the app reads in: an optional icon, a heading,
 * one paragraph that says what happened, then what the reader can do about it
 * (`children`).
 */
export function GateCard({
  icon,
  title,
  body,
  children,
}: {
  icon?: ReactNode
  title: ReactNode
  body: ReactNode
  children?: ReactNode
}) {
  return (
    <div className="w-full max-w-md space-y-4 rounded-card border p-6" style={GATE_CARD_STYLE}>
      {icon}
      <h1 className="m-0 text-heading font-semibold">{title}</h1>
      <p className="m-0 text-body" style={{ color: 'var(--fg-muted)' }}>
        {body}
      </p>
      {children}
    </div>
  )
}
