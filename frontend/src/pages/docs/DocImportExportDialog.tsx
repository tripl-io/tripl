import { useState } from 'react'
import { toast } from 'sonner'
import { Download, Loader2, Upload } from 'lucide-react'
import { ApiError } from '@/api/client'
import { docsApi } from '@/api/docs'
import { Checkbox } from '@/components/ui/checkbox'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Label } from '@/components/ui/label'
import { SegmentedControl } from '@/components/ui/segmented-control'
import { parseBundleFile } from '@/lib/docBundle'
import { formatBytes } from '@/lib/docTree'
import { getErrorMessage } from '@/lib/utils'
import type { DocImportMode, DocImportResult, DocScope, DocTreeLimits } from '@/types/docs'
import { useInvalidateDocs } from './useDocs'

/** The zip upload cap the server enforces (compressed). */
const MAX_ZIP_BYTES = 10 * 1024 * 1024

function download(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

/**
 * Export a scope as a zip (a folder of .md files, frontmatter intact — the
 * agent-skill layout round-trips) or a JSON bundle; import either back with a
 * dry-run preview first. Mirror also deletes notes missing from the upload.
 */
export function DocImportExportDialog({
  slug,
  open,
  onOpenChange,
  canEdit,
  isOrgAdmin,
  organizationName,
  limits,
}: {
  slug: string
  open: boolean
  onOpenChange: (open: boolean) => void
  canEdit: boolean
  /** An organization owner or admin: the only writers of organization notes. */
  isOrgAdmin: boolean
  organizationName: string
  limits: DocTreeLimits
}) {
  const [scope, setScope] = useState<DocScope>('project')
  const [mode, setMode] = useState<DocImportMode>('merge')
  const [keepRoot, setKeepRoot] = useState(false)
  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState<DocImportResult | null>(null)
  const [busy, setBusy] = useState<'export' | 'preview' | 'apply' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const invalidate = useInvalidateDocs(slug)
  // Organization notes are imported (merge or mirror) by organization owners
  // and admins only, the server's rule in services/docs_access.py.
  const orgBlocked = scope === 'organization' && !isOrgAdmin

  const reset = () => {
    setPreview(null)
    setError(null)
  }

  const exportAs = async (format: 'zip' | 'json') => {
    setBusy('export')
    setError(null)
    try {
      if (format === 'zip') {
        const { blob, filename } = await docsApi.exportZip(slug, scope)
        download(blob, filename)
      } else {
        const bundle = await docsApi.exportJson(slug, scope)
        const name = scope === 'organization' ? (bundle.organization_slug ?? slug) : bundle.project_slug
        download(new Blob([JSON.stringify(bundle, null, 2)], { type: 'application/json' }), `${name}-docs.json`)
      }
    } catch (err) {
      setError(getErrorMessage(err))
    } finally {
      setBusy(null)
    }
  }

  const runImport = async (dryRun: boolean) => {
    if (!file) return
    setBusy(dryRun ? 'preview' : 'apply')
    setError(null)
    try {
      const params = { scope, mode, dryRun }
      let result: DocImportResult
      if (/\.json$/i.test(file.name)) {
        const files = parseBundleFile(await file.text())
        result = await docsApi.importJson(slug, params, { format: 'tripl-docs/v1', files })
      } else {
        if (file.size > MAX_ZIP_BYTES) throw new Error(`The zip is ${formatBytes(file.size)}; the limit is 10 MB.`)
        result = await docsApi.importZip(slug, { ...params, keepRoot }, file)
      }
      setPreview(result)
      if (!dryRun) {
        await invalidate()
        toast.success(
          `Imported: ${result.created.length} created, ${result.updated.length} updated, ${result.deleted.length} deleted`,
        )
      }
    } catch (err) {
      setError(importErrorMessage(err))
    } finally {
      setBusy(null)
    }
  }

  const scopeOptions = [
    { value: 'project' as const, label: 'Project notes' },
    { value: 'organization' as const, label: `Organization · ${organizationName}` },
  ]

  return (
    <Dialog open={open} onOpenChange={next => { if (!next && busy === null) onOpenChange(false) }}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Import and export notes</DialogTitle>
          <DialogDescription>
            A folder of Markdown files with frontmatter; an agent skill (SKILL.md plus references/) imports as is.
            Non-.md files are skipped. Up to {limits.max_bundle_files} files and {formatBytes(limits.max_bundle_bytes)} per
            bundle.
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <span className="text-body-sm font-medium">Notes</span>
            <SegmentedControl<DocScope>
              aria-label="Scope"
              value={scope}
              onChange={value => {
                setScope(value)
                reset()
              }}
              options={scopeOptions}
            />
          </div>

          <section aria-labelledby="docs-export-heading" className="flex flex-col gap-2">
            <h3 id="docs-export-heading" className="micro-label m-0 text-fg-tertiary">
              Export
            </h3>
            <div className="flex flex-wrap gap-2">
              <Button variant="outline" size="sm" disabled={busy !== null} onClick={() => void exportAs('zip')}>
                <Download aria-hidden />
                Download .zip
              </Button>
              <Button variant="outline" size="sm" disabled={busy !== null} onClick={() => void exportAs('json')}>
                <Download aria-hidden />
                Download JSON bundle
              </Button>
            </div>
          </section>

          {canEdit && (
            <section aria-labelledby="docs-import-heading" className="flex flex-col gap-2">
              <h3 id="docs-import-heading" className="micro-label m-0 text-fg-tertiary">
                Import
              </h3>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="docs-import-file">File (.zip or .json)</Label>
                <input
                  id="docs-import-file"
                  type="file"
                  accept=".zip,.json,application/zip,application/json"
                  className="text-body-sm"
                  onChange={e => {
                    setFile(e.target.files?.[0] ?? null)
                    reset()
                  }}
                />
              </div>
              <SegmentedControl<DocImportMode>
                aria-label="Import mode"
                value={mode}
                onChange={value => {
                  setMode(value)
                  reset()
                }}
                options={[
                  { value: 'merge', label: 'Merge', title: 'Create and update; keep notes the upload does not have' },
                  { value: 'mirror', label: 'Mirror', title: 'Also delete notes the upload does not have' },
                ]}
              />
              {mode === 'mirror' && (
                <p className="m-0 text-caption text-warning">
                  Mirror deletes every {scope === 'organization' ? 'organization' : 'project'} note that is not in the
                  upload.
                </p>
              )}
              {orgBlocked && (
                <p className="m-0 text-caption text-fg-tertiary">
                  Only organization owners and admins can import organization notes.
                </p>
              )}
              {file && !/\.json$/i.test(file.name) && (
                <label className="flex items-center gap-2 text-body-sm">
                  <Checkbox checked={keepRoot} onCheckedChange={v => { setKeepRoot(v === true); reset() }} />
                  Keep the zip's top-level folder (by default a single wrapper folder is dropped)
                </label>
              )}
              <div className="flex flex-wrap gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={!file || busy !== null || orgBlocked}
                  onClick={() => void runImport(true)}
                >
                  {busy === 'preview' ? <Loader2 className="animate-spin" aria-hidden /> : <Upload aria-hidden />}
                  Preview (dry run)
                </Button>
                <Button
                  size="sm"
                  variant={mode === 'mirror' ? 'destructive' : 'default'}
                  disabled={!file || busy !== null || orgBlocked || !preview?.dry_run || preview.errors.length > 0}
                  onClick={() => void runImport(false)}
                >
                  {busy === 'apply' && <Loader2 className="animate-spin" aria-hidden />}
                  Import
                </Button>
              </div>
              {preview && <ImportPreview result={preview} />}
            </section>
          )}

          {error && (
            <p role="alert" className="m-0 whitespace-pre-wrap text-body-sm text-danger">
              {error}
            </p>
          )}
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={busy !== null}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function ImportPreview({ result }: { result: DocImportResult }) {
  const rows: { label: string; paths: string[]; tone?: string }[] = [
    { label: 'Create', paths: result.created },
    { label: 'Update', paths: result.updated },
    { label: 'Delete', paths: result.deleted, tone: 'text-danger' },
    { label: 'Unchanged', paths: result.unchanged },
    { label: 'Skipped', paths: result.skipped.map(s => `${s.path} — ${s.reason}`) },
    { label: 'Error', paths: result.errors.map(e => `${e.path} — ${e.detail}`), tone: 'text-danger' },
  ]
  return (
    <div className="rounded-control border border-border" aria-live="polite">
      <p className="m-0 border-b border-border px-3 py-2 text-body-sm font-medium">
        {result.dry_run ? 'Dry run — nothing was written' : 'Imported'}
      </p>
      <table className="w-full text-body-sm">
        <tbody>
          {rows
            .filter(row => row.paths.length > 0)
            .map(row => (
              <tr key={row.label} className="border-b border-border-subtle align-top last:border-b-0">
                <th scope="row" className={`w-28 px-3 py-1.5 text-left font-medium ${row.tone ?? ''}`}>
                  {row.label} <span className="tnum text-fg-tertiary">{row.paths.length}</span>
                </th>
                <td className="px-3 py-1.5">
                  <ul className="mono m-0 max-h-40 list-none overflow-y-auto p-0 text-caption">
                    {row.paths.map(path => (
                      <li key={path} className="truncate" title={path}>
                        {path}
                      </li>
                    ))}
                  </ul>
                </td>
              </tr>
            ))}
        </tbody>
      </table>
    </div>
  )
}

/** A 422 import carries `{errors: [{path, detail}]}`; list them. */
function importErrorMessage(err: unknown): string {
  if (err instanceof ApiError && err.detail && typeof err.detail === 'object') {
    const errors = (err.detail as { errors?: unknown }).errors
    if (Array.isArray(errors) && errors.length > 0) {
      const lines = errors.slice(0, 20).map(e => {
        const row = e as { path?: unknown; detail?: unknown }
        return `${String(row.path ?? '?')}: ${String(row.detail ?? '')}`
      })
      return `Nothing was imported:\n${lines.join('\n')}${errors.length > 20 ? `\n…and ${errors.length - 20} more` : ''}`
    }
  }
  return getErrorMessage(err)
}
