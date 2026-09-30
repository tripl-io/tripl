import { useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { ApiError } from '@/api/client'
import { projectsApi } from '@/api/projects'
import { ErrorState } from '@/components/error-state'
import { FieldError } from '@/components/forms/FieldError'
import { examplePlaceholder } from '@/components/forms/placeholders'
import { focusFirstInvalid, invalidAria } from '@/components/forms/validation'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { projectsKey } from '@/lib/queryKeys'
import { SLUG_ERROR, SLUG_HINT, isValidSlug, slugify } from '@/lib/slug'
import { ProjectTemplatePicker } from './ProjectTemplatePicker'
import { TEMPLATE_HINT } from './projectTemplateCopy'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

const SLUG_TAKEN_MESSAGE = 'Another project already uses this URL. Choose a different one.'
const TEMPLATE_HINT_ID = 'project-template-hint'

function isSlugConflict(error: unknown): error is ApiError {
  return error instanceof ApiError && error.status === 409
}

/**
 * The workspace page's "New project" dialog, titled like the button that
 * opens it (DS-29).
 *
 * The page mounts it only while it is open, so closing it (Cancel, Esc, the
 * overlay) throws the whole draft away: the fields, the "slug edited by hand"
 * flag and the last failed attempt. Reopening used to bring back the old name
 * and the old "Could not create project" error, with auto-slug switched off
 * for good (WS-18).
 */
export function CreateProjectDialog({
  onClose,
  existingSlugs,
}: {
  onClose: () => void
  /** Slugs already taken, so a name with no Latin letters gets a free `project-<n>`. */
  existingSlugs: readonly string[]
}) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [name, setName] = useState('')
  const [slug, setSlug] = useState('')
  const [slugTouched, setSlugTouched] = useState(false)
  // The URL field stays folded until asked for, or until it has a problem the
  // reader has to fix by hand (SH-29).
  const [customizing, setCustomizing] = useState(false)
  const [description, setDescription] = useState('')
  // Null is a blank project (F21, #274).
  const [templateId, setTemplateId] = useState<string | null>(null)
  // The server's word on the slug (a 409: taken). `existingSlugs` cannot rule it
  // out: the list hides seeding and failed demos, whose slugs are still held.
  // Cleared as soon as the slug changes.
  const [slugServerError, setSlugServerError] = useState<string | null>(null)
  const [submitted, setSubmitted] = useState(false)
  const formRef = useRef<HTMLFormElement>(null)

  const createMut = useMutation({
    // The dialog renders its own ErrorState, so the global toast stays quiet.
    meta: SILENT_ERROR_META,
    // A blank project sends the same body it always did.
    mutationFn: () =>
      projectsApi.create({
        name,
        slug,
        description,
        ...(templateId ? { template_id: templateId } : {}),
      }),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: projectsKey() })
      // Enter the freshly-created project instead of stranding the user on the
      // workspace list — mirrors the demo path's success routing (tripl-q7i1.8).
      // A template's plan is on a draft branch and main is empty, so the
      // overview would show nothing: open that branch's review instead (F21).
      void navigate(
        created.template_branch_id
          ? projectPath(currentOrgSlug(), created.slug, `/branches/${created.template_branch_id}`)
          : projectPath(currentOrgSlug(), created.slug, '/overview'),
      )
      onClose()
    },
    onError: (error) => {
      if (!isSlugConflict(error)) return
      // The only conflict on create is the slug. Open the field that fixes it
      // and put the message under it, instead of a generic box above a URL
      // the reader cannot edit without first finding "Customize URL" (SH-29).
      // The API says "slug"; this form calls the field Project URL.
      setSlugServerError(SLUG_TAKEN_MESSAGE)
      focusSlugField()
    },
  })

  // Focus lands on the URL field once it exists. It usually does not exist
  // yet (the button or the error opens it), and a frame-timed lookup lost the
  // race to React's commit on a busy machine, so the mount itself takes focus.
  const focusSlugOnMount = useRef(false)
  const focusSlugField = () => {
    const field = document.getElementById('project-slug')
    if (field) {
      field.focus()
      return
    }
    focusSlugOnMount.current = true
    setCustomizing(true)
  }
  const slugFieldRef = (field: HTMLInputElement | null) => {
    if (field && focusSlugOnMount.current) {
      focusSlugOnMount.current = false
      field.focus()
    }
  }

  const slugValid = isValidSlug(slug)
  // Said once the user has typed a slug or tried to submit, not while the
  // first keystroke of the name is still deriving one.
  const showSlugError = !slugValid && (submitted || (slugTouched && slug.length > 0))
  const nameError = submitted && !name.trim() ? 'Give the project a name.' : null
  // An empty name is the name's problem, not the URL's: the field opens only
  // for a slug the name could not derive a valid one for.
  const slugOpen = customizing || (showSlugError && name.trim() !== '')
  const slugMessage = showSlugError ? SLUG_ERROR : slugServerError

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
    >
      <DialogContent>
        <form
          ref={formRef}
          // No native `pattern` or `required`: the browser bubble named one
          // field at a time and never said what the format was (WS-17,
          // AU-4). Every problem is shown under its field instead, and the
          // first one takes focus.
          noValidate
          className="flex min-h-0 flex-col gap-4"
          onSubmit={(event) => {
            event.preventDefault()
            setSubmitted(true)
            if (!name.trim() || !slugValid) {
              if (name.trim() && !slugValid) setCustomizing(true)
              requestAnimationFrame(() => {
                if (formRef.current) focusFirstInvalid(formRef.current)
              })
              return
            }
            if (createMut.isPending) return
            createMut.mutate()
          }}
        >
          <DialogHeader>
            <DialogTitle>New project</DialogTitle>
          </DialogHeader>
          <DialogBody className="grid gap-4">
            <div className="grid gap-2">
              <Label htmlFor="project-name">Project name</Label>
              <Input
                id="project-name"
                value={name}
                onChange={(event) => {
                  setName(event.target.value)
                  if (!slugTouched) {
                    setSlug(slugify(event.target.value, existingSlugs))
                    setSlugServerError(null)
                  }
                }}
                // An example, not a value-looking default (MT-7).
                placeholder={examplePlaceholder('iOS app')}
                aria-required
                {...invalidAria('project-name', nameError)}
              />
              <FieldError inputId="project-name" message={nameError} className="mt-0" />
            </div>
            {/* The address, not "Slug (url-friendly)": derived from the name
                and shown as the URL it becomes, with the field itself one
                click away for anyone who wants a different one (SH-29). */}
            {slugOpen ? (
              <div className="grid gap-2">
                <Label htmlFor="project-slug">Project URL</Label>
                <div className="flex items-center gap-1.5">
                  <span aria-hidden="true" className="mono text-body-sm text-fg-tertiary">
                    /p/
                  </span>
                  <Input
                    id="project-slug"
                    ref={slugFieldRef}
                    value={slug}
                    onChange={(event) => {
                      setSlugTouched(true)
                      setSlug(event.target.value)
                      setSlugServerError(null)
                    }}
                    className="font-mono"
                    aria-required
                    aria-invalid={slugMessage ? true : undefined}
                    aria-describedby="project-slug-hint"
                  />
                </div>
                {slugMessage ? (
                  <FieldError id="project-slug-hint" message={slugMessage} className="mt-0" />
                ) : (
                  <p id="project-slug-hint" className="m-0 text-body-sm text-fg-tertiary">
                    {SLUG_HINT}
                  </p>
                )}
              </div>
            ) : (
              <p className="m-0 flex flex-wrap items-center gap-x-2 gap-y-1 text-body-sm text-fg-tertiary">
                <span>
                  Project URL:{' '}
                  <span className="mono text-fg">
                    /p/{slug || 'your-project'}
                  </span>
                </span>
                <Button
                  type="button"
                  variant="link"
                  size="xs"
                  className="h-auto px-0"
                  onClick={() => {
                    // The field replaces this button, so focus follows to it.
                    focusSlugField()
                  }}
                >
                  Customize URL
                </Button>
              </p>
            )}
            <div className="grid gap-2">
              <Label htmlFor="project-desc" optional>
                Description
              </Label>
              {/* One line to start: it is optional, so it should not look as
                  weighty as the name (SH-29). */}
              <Textarea
                id="project-desc"
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                rows={1}
                className="min-h-8"
              />
            </div>
            <ProjectTemplatePicker
              value={templateId}
              onChange={setTemplateId}
              disabled={createMut.isPending}
              describedBy={templateId ? TEMPLATE_HINT_ID : undefined}
            />
            {templateId && (
              <p id={TEMPLATE_HINT_ID} className="m-0 text-body-sm text-fg-tertiary">
                {TEMPLATE_HINT}
              </p>
            )}
            {/* A slug conflict is told under the URL field, not here — also
                once the reader has edited the slug and cleared it there. */}
            {createMut.isError && !isSlugConflict(createMut.error) && (
              <ErrorState compact title="Could not create project" error={createMut.error} />
            )}
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={createMut.isPending}>
              {createMut.isPending ? 'Creating…' : templateId ? 'Create from template' : 'Create'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
