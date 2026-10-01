import { useState, type FormEvent } from 'react'
import { toast } from 'sonner'
import { AlertTriangle, Bot, Languages, Loader2, RotateCcw, Trash2 } from 'lucide-react'
import { ApiError } from '@/api/client'
import { Chip } from '@/components/primitives/chip'
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
import { IconButton } from '@/components/ui/icon-button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { formatDateTime } from '@/lib/datetime'
import { languageName, ORIGINAL_LANG } from '@/lib/docLanguages'
import { getErrorMessage } from '@/lib/utils'
import type {
  DocFileResponse,
  DocLanguageDefaults,
  DocScope,
  DocTranslationRevisionSummary,
  DocTranslationSummary,
} from '@/types/docs'
import {
  useRemoveTranslation,
  useRestoreTranslationRevision,
  useTranslateDoc,
  useTranslationRevisions,
  useUpdateDocLanguages,
  useWriteTranslation,
} from './useDocs'

function statusSuffix(translation: DocTranslationSummary): string {
  if (translation.status === 'pending') return ' · translating…'
  if (translation.status === 'failed' && translation.revision === 0) return ' · failed'
  if (translation.outdated) return ' · outdated'
  return ''
}

/** A 409 refusing to translate over a person's edits without `overwrite`. */
function editedRefusal(err: unknown): string | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null
  const detail = err.detail as { code?: unknown; message?: unknown } | undefined
  if (detail?.code !== 'translation_edited') return null
  return typeof detail.message === 'string' ? detail.message : 'This translation was edited by hand.'
}

/**
 * The language row of an open note: which language is shown (the original or
 * a stored translation), "Translate with AI" for editors, and the controls of
 * the translation on screen.
 */
export function DocLanguageBar({
  slug,
  doc,
  lang,
  canEdit,
  onChangeLang,
}: {
  slug: string
  doc: DocFileResponse
  /** What the page asked for: a code or `original`. */
  lang: string
  canEdit: boolean
  onChangeLang: (lang: string) => void
}) {
  const [translateOpen, setTranslateOpen] = useState(false)
  const remove = useRemoveTranslation(slug)
  const translations = doc.translations ?? []
  const shown = doc.lang ? translations.find(t => t.lang === doc.lang) : undefined
  const options = [...new Set([ORIGINAL_LANG, ...translations.map(t => t.lang), lang])]

  const onRemove = async () => {
    if (!doc.lang) return
    try {
      await remove.mutateAsync({ scope: doc.scope, path: doc.path, lang: doc.lang })
      toast.success(`Deleted the ${languageName(doc.lang)} translation`)
      onChangeLang(ORIGINAL_LANG)
    } catch (err) {
      toast.error(getErrorMessage(err))
    }
  }

  if (options.length === 1 && !canEdit) return null
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Languages className="size-4 text-fg-tertiary" aria-hidden />
      <Select value={lang} onValueChange={onChangeLang}>
        <SelectTrigger aria-label="Language" className="h-(--control-h) w-auto min-w-40">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {options.map(code => {
            const translation = translations.find(t => t.lang === code)
            const suffix = translation ? statusSuffix(translation) : code === ORIGINAL_LANG ? '' : ' · none yet'
            return (
              <SelectItem key={code} value={code}>
                {languageName(code)}
                {suffix}
              </SelectItem>
            )
          })}
        </SelectContent>
      </Select>
      {shown?.machine && (
        <Chip size="xs" tone="info" icon={<Bot className="size-3" aria-hidden />}>
          AI translation
        </Chip>
      )}
      {shown && !shown.machine && (
        <Chip size="xs" variant="outline">
          Edited by hand
        </Chip>
      )}
      {canEdit && (
        <>
          <Button variant="outline" size="sm" onClick={() => setTranslateOpen(true)}>
            <Bot aria-hidden />
            Translate with AI
          </Button>
          {doc.lang && (
            <IconButton
              label={`Delete the ${languageName(doc.lang)} translation`}
              size="icon-sm"
              variant="danger"
              onClick={() => void onRemove()}
              disabled={remove.isPending}
            >
              <Trash2 />
            </IconButton>
          )}
        </>
      )}
      <TranslateDialog
        slug={slug}
        doc={doc}
        open={translateOpen}
        onOpenChange={setTranslateOpen}
        onStarted={onChangeLang}
      />
    </div>
  )
}

/**
 * Why the page shows what it shows, when that is not simply the language
 * asked for: a translation behind the original, or none to show yet.
 */
export function DocTranslationNotice({
  slug,
  doc,
  canEdit,
  onChangeLang,
}: {
  slug: string
  doc: DocFileResponse
  canEdit: boolean
  onChangeLang: (lang: string) => void
}) {
  const translate = useTranslateDoc(slug)
  const write = useWriteTranslation(slug)
  const [replacing, setReplacing] = useState<string | null>(null)
  const shown = doc.lang ? doc.translations?.find(t => t.lang === doc.lang) : undefined
  const wanted = doc.requested_lang ? doc.translations?.find(t => t.lang === doc.requested_lang) : undefined

  const translateAgain = async (code: string, overwrite = false) => {
    try {
      await translate.mutateAsync({ scope: doc.scope, path: doc.path, language: code, overwrite })
      setReplacing(null)
      toast.success(`Translating into ${languageName(code)}…`)
    } catch (err) {
      const edited = editedRefusal(err)
      if (edited) setReplacing(code)
      else toast.error(getErrorMessage(err))
    }
  }

  const markCurrent = async () => {
    if (!doc.lang || !shown) return
    try {
      await write.mutateAsync({
        scope: doc.scope,
        path: doc.path,
        lang: doc.lang,
        content: doc.content,
        base_revision: shown.revision,
        mark_current: true,
      })
      toast.success('Marked as up to date')
    } catch (err) {
      toast.error(getErrorMessage(err))
    }
  }

  const replaceDialog = (
    <Dialog open={replacing !== null} onOpenChange={open => { if (!open) setReplacing(null) }}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Replace the edited translation?</DialogTitle>
          <DialogDescription>
            The {languageName(replacing ?? '')} translation has been edited by hand. Translating again replaces it;
            the edited text stays in its history and can be restored.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={() => setReplacing(null)}>
            Keep it
          </Button>
          <Button
            variant="destructive"
            onClick={() => void translateAgain(replacing ?? '', true)}
            disabled={translate.isPending}
          >
            {translate.isPending && <Loader2 className="animate-spin" aria-hidden />}
            Translate again
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )

  if (doc.lang && doc.translation_outdated && shown) {
    return (
      <div role="status" className="rounded-control border border-warning bg-warning-soft px-3 py-2 text-body-sm">
        <p className="m-0 flex items-center gap-1.5 font-medium text-warning">
          <AlertTriangle className="size-3.5" aria-hidden />
          This translation is behind the original
        </p>
        <p className="m-0 mt-1 text-fg-secondary">
          It was made from revision {shown.source_revision}; the original is at revision {doc.revision}. Agents
          that read this note without naming a language get the original until the translation is current.
        </p>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {canEdit && (
            <>
              <Button
                size="sm"
                variant="outline"
                onClick={() => void translateAgain(shown.lang)}
                disabled={translate.isPending}
              >
                {translate.isPending && <Loader2 className="animate-spin" aria-hidden />}
                Translate again
              </Button>
              <Button size="sm" variant="outline" onClick={() => void markCurrent()} disabled={write.isPending}>
                Mark as up to date
              </Button>
            </>
          )}
          <Button size="sm" variant="ghost" onClick={() => onChangeLang(ORIGINAL_LANG)}>
            Show the original
          </Button>
        </div>
        {replaceDialog}
      </div>
    )
  }
  if (!doc.lang && doc.requested_lang && doc.translation_fallback) {
    const code = doc.requested_lang
    const name = languageName(code)
    const pending = doc.translation_fallback === 'pending'
    const failed = doc.translation_fallback === 'failed'
    return (
      <div role="status" className="rounded-control border border-border bg-bg-sunken px-3 py-2 text-body-sm">
        <p className="m-0 flex items-center gap-1.5 font-medium">
          {pending && <Loader2 className="size-3.5 animate-spin" aria-hidden />}
          {pending
            ? `Translating into ${name}…`
            : failed
              ? `The ${name} translation failed`
              : `There is no ${name} translation of this note yet`}
        </p>
        <p className="m-0 mt-1 text-fg-secondary">
          {failed && wanted?.error ? `${wanted.error}. ` : ''}
          The original is shown{pending ? ' until it is ready' : ''}.
        </p>
        {canEdit && !pending && (
          <Button
            className="mt-2"
            size="sm"
            variant="outline"
            onClick={() => void translateAgain(code)}
            disabled={translate.isPending}
          >
            {translate.isPending ? <Loader2 className="animate-spin" aria-hidden /> : <Bot aria-hidden />}
            {failed ? 'Try again' : `Translate into ${name} with AI`}
          </Button>
        )}
        {replaceDialog}
      </div>
    )
  }
  return null
}

/** "Which language?": the person types a name or a code, the model makes it a code. */
export function TranslateDialog({
  slug,
  doc,
  open,
  onOpenChange,
  onStarted,
}: {
  slug: string
  doc: Pick<DocFileResponse, 'scope' | 'path'>
  open: boolean
  onOpenChange: (open: boolean) => void
  onStarted: (lang: string) => void
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {open && <TranslateForm slug={slug} doc={doc} onClose={() => onOpenChange(false)} onStarted={onStarted} />}
    </Dialog>
  )
}

function TranslateForm({
  slug,
  doc,
  onClose,
  onStarted,
}: {
  slug: string
  doc: Pick<DocFileResponse, 'scope' | 'path'>
  onClose: () => void
  onStarted: (lang: string) => void
}) {
  const [language, setLanguage] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [edited, setEdited] = useState<string | null>(null)
  const translate = useTranslateDoc(slug)

  const run = async (overwrite: boolean) => {
    setError(null)
    try {
      const started = await translate.mutateAsync({
        scope: doc.scope,
        path: doc.path,
        language: language.trim(),
        overwrite,
      })
      toast.success(`Translating into ${languageName(started.lang)}…`)
      onStarted(started.lang)
      onClose()
    } catch (err) {
      const refusal = editedRefusal(err)
      if (refusal) setEdited(refusal)
      else setError(getErrorMessage(err))
    }
  }

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (language.trim()) void run(false)
  }

  return (
    <DialogContent>
      <form onSubmit={submit} className="flex min-h-0 flex-col gap-4">
        <DialogHeader>
          <DialogTitle>Translate with AI</DialogTitle>
          <DialogDescription>
            The note is translated once with your organization&apos;s AI key and stored. You can edit the
            translation afterwards; it does not change when the original does.
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="flex flex-col gap-1.5">
          <Label htmlFor="translate-language">Which language?</Label>
          <Input
            id="translate-language"
            value={language}
            onChange={e => {
              setLanguage(e.target.value.slice(0, 64))
              setEdited(null)
            }}
            placeholder="English, Deutsch, pt-BR…"
            aria-invalid={error !== null}
          />
          <p className="m-0 text-caption text-fg-tertiary">
            A name in any language, or a code. It is stored as a code (German: de).
          </p>
          {error && (
            <p role="alert" className="m-0 text-body-sm text-danger">
              {error}
            </p>
          )}
          {edited !== null && (
            <p role="alert" className="m-0 text-body-sm text-warning">
              {edited}
            </p>
          )}
        </DialogBody>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>
            Cancel
          </Button>
          {edited !== null ? (
            <Button type="button" variant="destructive" onClick={() => void run(true)} disabled={translate.isPending}>
              {translate.isPending && <Loader2 className="animate-spin" aria-hidden />}
              Replace it
            </Button>
          ) : (
            <Button type="submit" disabled={translate.isPending || !language.trim()}>
              {translate.isPending && <Loader2 className="animate-spin" aria-hidden />}
              Translate
            </Button>
          )}
        </DialogFooter>
      </form>
    </DialogContent>
  )
}

const ACTION_LABEL: Record<DocTranslationRevisionSummary['action'], string> = {
  translate: 'AI translation',
  edit: 'Edited',
  restore: 'Restored',
}

/** Every saved state of one translation, newest first; an older one can be restored. */
export function TranslationHistoryDialog({
  slug,
  scope,
  path,
  lang,
  open,
  onOpenChange,
  canEdit,
}: {
  slug: string
  scope: DocScope
  path: string
  lang: string
  open: boolean
  onOpenChange: (open: boolean) => void
  canEdit: boolean
}) {
  const revisions = useTranslationRevisions(slug, scope, path, lang, open)
  const restore = useRestoreTranslationRevision(slug)

  const onRestore = async (revision: DocTranslationRevisionSummary) => {
    try {
      await restore.mutateAsync({ scope, path, revisionId: revision.id })
      toast.success(`Restored revision ${revision.number}`)
      onOpenChange(false)
    } catch (err) {
      toast.error(getErrorMessage(err))
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{languageName(lang)} translation history</DialogTitle>
          <DialogDescription>Every saved version of this translation, newest first.</DialogDescription>
        </DialogHeader>
        <DialogBody>
          {revisions.isPending ? (
            <p className="m-0 text-body-sm text-fg-tertiary">Loading…</p>
          ) : revisions.isError ? (
            <p role="alert" className="m-0 text-body-sm text-danger">
              {getErrorMessage(revisions.error)}
            </p>
          ) : (
            <ol className="m-0 flex list-none flex-col gap-1 p-0">
              {revisions.data.map((revision, index) => (
                <li key={revision.id} className="flex items-center gap-2 rounded-control px-2 py-1 hover:bg-surface-hover">
                  <span className="tnum w-8 text-caption text-fg-tertiary">#{revision.number}</span>
                  <span className="min-w-0 flex-1 text-body-sm">
                    {ACTION_LABEL[revision.action]} · from original revision {revision.source_revision}
                    <span className="block text-caption text-fg-tertiary">
                      {formatDateTime(revision.created_at)}
                      {revision.author_name ? ` · ${revision.author_name}` : ''}
                    </span>
                  </span>
                  {canEdit && index > 0 && (
                    <IconButton
                      label={`Restore revision ${revision.number}`}
                      size="icon-sm"
                      variant="ghost"
                      onClick={() => void onRestore(revision)}
                      disabled={restore.isPending}
                    >
                      <RotateCcw />
                    </IconButton>
                  )}
                </li>
              ))}
            </ol>
          )}
        </DialogBody>
      </DialogContent>
    </Dialog>
  )
}

const CODE = /^[a-z]{2}(-[a-z0-9]{2,8})*$/i

/** What a default field resolves to, as far as the page can tell before saving. */
function defaultHint(value: string): string {
  const typed = value.trim()
  if (!typed) return 'The original.'
  return CODE.test(typed) ? `${languageName(typed.toLowerCase())}.` : 'A name: the AI model turns it into a code on save.'
}

/** The project's default languages: what agents read, and what the app opens for people. */
export function DocLanguagesDialog({
  slug,
  defaults,
  open,
  onOpenChange,
  canEdit,
}: {
  slug: string
  defaults: DocLanguageDefaults
  open: boolean
  onOpenChange: (open: boolean) => void
  canEdit: boolean
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {open && (
        <LanguagesForm slug={slug} defaults={defaults} onClose={() => onOpenChange(false)} canEdit={canEdit} />
      )}
    </Dialog>
  )
}

function LanguagesForm({
  slug,
  defaults,
  onClose,
  canEdit,
}: {
  slug: string
  defaults: DocLanguageDefaults
  onClose: () => void
  canEdit: boolean
}) {
  const [agent, setAgent] = useState(defaults.agent_lang ?? '')
  const [human, setHuman] = useState(defaults.human_lang ?? '')
  const [error, setError] = useState<string | null>(null)
  const update = useUpdateDocLanguages(slug)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    try {
      await update.mutateAsync({ agent_lang: agent.trim() || null, human_lang: human.trim() || null })
      toast.success('Default languages saved')
      onClose()
    } catch (err) {
      setError(getErrorMessage(err))
    }
  }

  return (
    <DialogContent>
      <form onSubmit={e => void submit(e)} className="flex min-h-0 flex-col gap-4">
        <DialogHeader>
          <DialogTitle>Note languages</DialogTitle>
          <DialogDescription>
            Which stored translation each reader gets by default. A note without that translation, or with one
            behind the original, is shown in its original language.
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="docs-agent-lang">For agents (API, MCP, CLI)</Label>
            <Input
              id="docs-agent-lang"
              value={agent}
              onChange={e => setAgent(e.target.value.slice(0, 64))}
              placeholder="Empty: the original"
              disabled={!canEdit}
            />
            <p className="m-0 text-caption text-fg-tertiary">
              {defaultHint(agent)} An agent can still ask for any language, or the original.
            </p>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="docs-human-lang">For people (this app)</Label>
            <Input
              id="docs-human-lang"
              value={human}
              onChange={e => setHuman(e.target.value.slice(0, 64))}
              placeholder="Empty: the original"
              disabled={!canEdit}
            />
            <p className="m-0 text-caption text-fg-tertiary">
              {defaultHint(human)} Anyone can switch language on a note; the choice is kept in their browser.
            </p>
          </div>
          {error && (
            <p role="alert" className="m-0 text-body-sm text-danger">
              {error}
            </p>
          )}
        </DialogBody>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>
            {canEdit ? 'Cancel' : 'Close'}
          </Button>
          {canEdit && (
            <Button type="submit" disabled={update.isPending}>
              {update.isPending && <Loader2 className="animate-spin" aria-hidden />}
              Save
            </Button>
          )}
        </DialogFooter>
      </form>
    </DialogContent>
  )
}
