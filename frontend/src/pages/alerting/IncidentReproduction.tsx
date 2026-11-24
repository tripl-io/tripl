import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'

const verdicts = ['reproduced', 'verified', 'still_failing', 'inconclusive', 'no_regression']

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function label(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0 && value.length <= 200 &&
    !/\p{C}/u.test(value)
}

function command(event: string, candidate: boolean) {
  // Bash/POSIX single quoting protects every character, including an embedded quote.
  const quoted = "'" + event.replaceAll("'", "'\\''") + "'"
  return `tripl reproduce --event=${quoted} --healthy healthy.json --suspect suspect.json${candidate ? ' --candidate candidate.json' : ''} --json`
}

function buildLine(name: string, entry: unknown) {
  if (!record(entry) || !label(entry.build)) throw new Error(`Invalid ${name} build.`)
  const count = entry.count
  if (count !== null && !(typeof count === 'number' && Number.isSafeInteger(count) && count >= 0)) {
    throw new Error(`Invalid ${name} event count.`)
  }
  return `${name}: ${entry.build} (${count ?? 'inconclusive'} events)`
}

const roles = ['healthy', 'suspect', 'candidate'] as const
type Role = typeof roles[number]
type ArtifactLinks = Record<Role, string>

function artifactLink(value: string, role: Role) {
  let url: URL
  try {
    url = new URL(value)
  } catch {
    throw new Error(`Enter a stable HTTPS artifact page for the ${role} build.`)
  }
  if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash ||
      !label(value) || /\s/u.test(value)) {
    throw new Error('Use an HTTPS artifact page without credentials, query parameters or fragments.')
  }
  return url.href
}

function reportNote(text: string, event: string, links: ArtifactLinks) {
  const report: unknown = JSON.parse(text)
  if (!record(report) || report.version !== 1 || report.event !== event ||
      !label(report.verdict) || !verdicts.includes(report.verdict)) {
    throw new Error('Use a version 1 report for the selected event.')
  }
  const inconclusive = report.verdict === 'inconclusive'
  const lines = [`Reproduction: ${report.verdict} — ${event}`]
  for (const key of ['journey', 'fixture'] as const) {
    if (inconclusive && report[key] === null) continue
    if (!label(report[key])) throw new Error(`Invalid ${key}.`)
    lines.push(`${key}: ${report[key]}`)
  }
  if (inconclusive && label(report.reason)) lines.push(report.reason)
  for (const role of roles) {
    const entry = report[role]
    if (role === 'candidate' && entry == null && !links.candidate) continue
    if (inconclusive && entry === null) {
      lines.push(`${role}: missing capture`)
      if (!links[role]) continue
    } else {
      lines.push(buildLine(role, entry))
    }
    lines.push(`${role}.json: ${artifactLink(links[role], role)}`)
  }
  lines.push(command(event, report.candidate != null || !!links.candidate))
  return lines.join('\n')
}

/** Existing incident-note permissions and save flow govern the final attachment. */
export function IncidentReproduction({ initialEvent, append }: {
  initialEvent: string
  append: (note: string) => void
}) {
  const [open, setOpen] = useState(false)
  const [event, setEvent] = useState(initialEvent)
  const [report, setReport] = useState('')
  const [links, setLinks] = useState<ArtifactLinks>({ healthy: '', suspect: '', candidate: '' })
  const [error, setError] = useState('')
  function addReport() {
    try {
      append(reportNote(report, event, links))
      setReport('')
      setError('')
    } catch (cause) {
      setError(cause instanceof SyntaxError ? 'Paste valid JSON from tripl reproduce.' :
        cause instanceof Error ? cause.message : 'Unable to read this report.')
    }
  }
  return (
    <div className="mt-2 text-body-sm">
      <Button type="button" variant="outline" size="sm" aria-expanded={open} onClick={() => setOpen(!open)}>
        Reproduce tracking regression
      </Button>
      {open && <div className="mt-2 grid gap-2">
        <label>Tracked event<Input value={event} maxLength={200} onChange={e => setEvent(e.target.value)} /></label>
        <p>Run the same synthetic journey on both builds. Save the captures as healthy.json and suspect.json, then compare locally.</p>
        {label(event) && <pre className="overflow-x-auto whitespace-pre-wrap break-all">{command(event, false)}</pre>}
        <p>Link the stable CI artifact pages so another engineer can download the same captures. Use candidate.json when checking a fix.</p>
        {roles.map(role => (
          <label key={role}>{role} artifact page{role === 'candidate' ? ' (for a fix)' : ''}
            <Input type="url" maxLength={200} value={links[role]}
              onChange={e => setLinks({ ...links, [role]: e.target.value })} />
          </label>
        ))}
        <label>CLI report JSON<Textarea value={report} maxLength={16000} onChange={e => setReport(e.target.value)} /></label>
        {error && <p role="alert" className="text-danger">{error}</p>}
        <Button type="button" size="sm" variant="outline" disabled={!label(event) || !report.trim()} onClick={addReport}>
          Add evidence to note draft
        </Button>
      </div>}
    </div>
  )
}
