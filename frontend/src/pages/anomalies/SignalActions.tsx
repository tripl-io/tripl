import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import {
  BellOff,
  BellRing,
  Bug,
  CalendarPlus,
  Check,
  CircleAlert,
  CircleCheck,
  CircleSlash,
  ExternalLink,
  MessageSquarePlus,
  MoreHorizontal,
  Undo2,
  type LucideIcon,
} from 'lucide-react'

import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { useConfirm } from '@/hooks/useConfirm'
import { getAlertingPath } from '@/lib/navigation'
import { useCanWriteProject } from '@/lib/permissions'
import { signalScopeLabel, unnamedScopeLabel } from '@/lib/signalScope'
import { signalCommentDraft } from '@/lib/signalVerdict'
import type { MonitoringSignal, SignalVerdictKind } from '@/types'
import { SignalVerdictDialog } from './SignalVerdictDialog'
import {
  CLEAR_AND_REOPEN_CONFIRM,
  MUTE_OPTIONS,
  canClearVerdict,
  canSetVerdict,
  canTriageSignal,
  clearVerdictLabel,
  clearVerdictReopensIncident,
  signalIncidentId,
  useSignalTriage,
} from './signalTriage'

const ICON_CLASS = 'h-3.5 w-3.5 shrink-0'
const ICON_STYLE = { color: 'var(--fg-subtle)' }

/** The row menu's verdict items: the dialog then asks the reason and a note. */
const VERDICT_ITEMS: ReadonlyArray<{ verdict: SignalVerdictKind; label: string; icon: LucideIcon }> = [
  { verdict: 'expected', label: 'Mark as expected…', icon: CircleCheck },
  { verdict: 'tracking_bug', label: 'Tracking bug…', icon: Bug },
  { verdict: 'false_positive', label: 'False positive…', icon: CircleSlash },
  { verdict: 'real_issue', label: 'Real issue…', icon: CircleAlert },
]

/**
 * The row's action menu (MO-4 / JR-5, #254): open the detail, jump to the
 * incident or the alerts, annotate the bucket — and, for a signal no rule
 * routed to an incident, acknowledge it or mute its scope, each with its Undo.
 * Any signal takes a verdict here (expected, tracking bug, false positive, real
 * issue); on a routed one the verdict is written to its incident, and
 * clearing it reopens that incident, so the clear says so and asks first. The
 * verdicts are a radio group, so assistive tech hears which one is current. A
 * tracking bug on an event offers the event's discussion, prefilled. Mute durations and
 * verdicts are labelled groups rather than submenus: the menu primitive has
 * none, on purpose (DS-36).
 */
export function SignalActions({
  slug,
  signal,
  href,
}: {
  slug: string
  signal: MonitoringSignal
  href: string | undefined
}) {
  const navigate = useNavigate()
  const canWrite = useCanWriteProject()
  const triage = useSignalTriage(slug)
  const { confirm, dialog } = useConfirm()
  const [verdictOpen, setVerdictOpen] = useState<SignalVerdictKind | null>(null)
  const triageable = canWrite && canTriageSignal(signal)
  const verdictable = canWrite && canSetVerdict(signal)
  const incidentId = signalIncidentId(signal)
  // The event's discussion, where a tracking bug is raised with whoever owns
  // the instrumentation; only an event-scope row has one event to go to.
  const commentHref =
    canWrite && href && signal.scope_type === 'event' && signal.verdict?.verdict === 'tracking_bug'
      ? href
      : undefined
  const clearVerdict = async () => {
    if (clearVerdictReopensIncident(signal) && !(await confirm(CLEAR_AND_REOPEN_CONFIRM))) return
    triage.run(signal, { kind: 'clearVerdict' })
  }
  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="ghost" size="icon-sm" aria-label="Signal actions" className="text-fg-muted">
            <MoreHorizontal aria-hidden="true" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" sideOffset={6} className="w-52">
          {href && (
            <DropdownMenuItem asChild className="text-body-sm">
              <Link to={href}>
                <ExternalLink className={ICON_CLASS} style={ICON_STYLE} /> Open detail
              </Link>
            </DropdownMenuItem>
          )}
          <DropdownMenuItem asChild className="text-body-sm">
            <Link to={getAlertingPath(slug, { incidentId })}>
              <BellRing className={ICON_CLASS} style={ICON_STYLE} />{' '}
              {incidentId ? 'Open incident' : 'View alerts'}
            </Link>
          </DropdownMenuItem>
          {/* The detail page's banner Annotate, from here: its Volume tab with
              the form prefilled on this bucket (JR-5). */}
          {href && canWrite && (
            <DropdownMenuItem
              className="text-body-sm"
              onSelect={() => navigate(href, { state: { annotateBucket: signal.bucket } })}
            >
              <CalendarPlus className={ICON_CLASS} style={ICON_STYLE} /> Annotate
            </DropdownMenuItem>
          )}
          {triageable && (
            <>
              <DropdownMenuSeparator />
              {signal.acknowledged_at ? (
                <DropdownMenuItem
                  className="text-body-sm"
                  disabled={triage.isPending}
                  onSelect={() => triage.run(signal, { kind: 'unacknowledge' })}
                >
                  <Undo2 className={ICON_CLASS} style={ICON_STYLE} /> Undo acknowledge
                </DropdownMenuItem>
              ) : (
                <DropdownMenuItem
                  className="text-body-sm"
                  disabled={triage.isPending}
                  onSelect={() => triage.run(signal, { kind: 'acknowledge' })}
                >
                  <Check className={ICON_CLASS} style={ICON_STYLE} /> Acknowledge
                </DropdownMenuItem>
              )}
              {signal.muted ? (
                <DropdownMenuItem
                  className="text-body-sm"
                  disabled={triage.isPending}
                  onSelect={() => triage.run(signal, { kind: 'unmute' })}
                >
                  <Undo2 className={ICON_CLASS} style={ICON_STYLE} /> Unmute scope
                </DropdownMenuItem>
              ) : (
                <DropdownMenuGroup aria-label="Mute this scope">
                  <DropdownMenuLabel className="flex items-center gap-2 text-caption font-medium text-fg-tertiary">
                    <BellOff className={ICON_CLASS} aria-hidden="true" /> Mute this scope
                  </DropdownMenuLabel>
                  {MUTE_OPTIONS.map((option) => (
                    <DropdownMenuItem
                      key={option.duration}
                      inset
                      aria-label={`Mute ${option.label.toLowerCase()}`}
                      className="text-body-sm"
                      disabled={triage.isPending}
                      onSelect={() => triage.run(signal, { kind: 'mute', duration: option.duration })}
                    >
                      {option.label}
                    </DropdownMenuItem>
                  ))}
                </DropdownMenuGroup>
              )}
            </>
          )}
          {verdictable && (
            <>
              <DropdownMenuSeparator />
              {/* A radio group: each item is a menuitemradio whose aria-checked
                  tells assistive tech which verdict is current. Picking one
                  (the current one too) opens the dialog for its reason and
                  note; the group's value only follows the saved verdict. */}
              <DropdownMenuRadioGroup aria-label="Verdict" value={signal.verdict?.verdict ?? ''}>
                <DropdownMenuLabel className="text-caption font-medium text-fg-tertiary">
                  Verdict
                </DropdownMenuLabel>
                {VERDICT_ITEMS.map((item) => {
                  const Icon = item.icon
                  return (
                    <DropdownMenuRadioItem
                      key={item.verdict}
                      value={item.verdict}
                      className="text-body-sm"
                      disabled={triage.isPending}
                      onSelect={() => setVerdictOpen(item.verdict)}
                    >
                      <Icon className={ICON_CLASS} style={ICON_STYLE} aria-hidden="true" /> {item.label}
                    </DropdownMenuRadioItem>
                  )
                })}
              </DropdownMenuRadioGroup>
              {canClearVerdict(signal) && (
                <DropdownMenuItem
                  className="text-body-sm"
                  disabled={triage.isPending}
                  onSelect={() => void clearVerdict()}
                >
                  <Undo2 className={ICON_CLASS} style={ICON_STYLE} /> {clearVerdictLabel(signal)}
                </DropdownMenuItem>
              )}
            </>
          )}
          {commentHref && (
            <DropdownMenuItem
              className="text-body-sm"
              onSelect={() =>
                navigate(commentHref, {
                  state: { commentDraft: signalCommentDraft(signal, signal.verdict?.note) },
                })
              }
            >
              <MessageSquarePlus className={ICON_CLASS} style={ICON_STYLE} /> Open a comment on the event
            </DropdownMenuItem>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
      {verdictOpen && (
        <SignalVerdictDialog
          verdict={verdictOpen}
          bucket={signal.bucket}
          scopeLabel={signalScopeLabel(signal) ?? unnamedScopeLabel(signal)}
          routed={incidentId !== null}
          pending={triage.isPending}
          onClose={() => setVerdictOpen(null)}
          onConfirm={({ expectedReason, note }) =>
            triage.run(
              signal,
              { kind: 'verdict', verdict: verdictOpen, expectedReason, note },
              () => setVerdictOpen(null),
            )
          }
        />
      )}
      {dialog}
    </>
  )
}
