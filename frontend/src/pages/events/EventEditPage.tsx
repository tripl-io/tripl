import { useEffect, useState } from 'react'
import { Link, Navigate, useLocation, useNavigate, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { toast } from 'sonner'
import type { EventMutationResponse, EventType, MetaFieldDefinition, Variable } from '@/types'
import { eventCommentsApi } from '@/api/eventComments'
import { eventsApi } from '@/api/events'
import { eventTypesApi } from '@/api/eventTypes'
import { metaFieldsApi } from '@/api/metaFields'
import { planBranchesApi } from '@/api/planBranches'
import { variablesApi } from '@/api/variables'
import { useActiveBranchId, useBranchLinkProps } from '@/hooks/useBranch'
import { displayUser, useUsersById } from '@/hooks/useUsersById'
import { CommentThread } from '@/components/comment-thread'
import { EntityBranchBanner } from '@/components/EntityBranchBanner'
import { ErrorState } from '@/components/error-state'
import { PageContainer } from '@/components/primitives/page-container'
import { PageSkeleton, QueryErrorState } from '@/components/states'
import { Button } from '@/components/ui/button'
import { useCanWriteProject } from '@/lib/permissions'
import {
  eventCommentsKey,
  eventKey,
  eventTypesKey,
  metaFieldsKey,
  planBranchesKey,
  variablesKey,
} from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import { rememberCreatedEvents } from './createdEventsHandoff'
import { DraftDiscussionNote } from './DraftDiscussionNote'
import { EventForm } from './EventFormView'
import { EventHealthCard } from './EventHealthCard'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

const EMPTY_EVENT_TYPES: EventType[] = []
const EMPTY_META_FIELDS: MetaFieldDefinition[] = []
const EMPTY_VARIABLES: Variable[] = []

/**
 * Page-based route wrapper: loads the data EventForm needs (event types, meta
 * fields, variables, and — when editing — the event itself) and renders the
 * form full-page. Reached via `/p/:slug/events/:tab/new` and
 * `/p/:slug/events/:tab/:eventId/edit`. Replaces the old inline Sheet.
 */
export default function EventEditPage() {
  const { slug, tab, eventId } = useParams<{ slug: string; tab?: string; eventId?: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const branchId = useActiveBranchId()
  const usersById = useUsersById()
  const branchLink = useBranchLinkProps()
  const canWrite = useCanWriteProject()
  const isNew = !eventId
  const listPath = !tab || tab === 'all' ? projectPath(currentOrgSlug(), slug, '/events') : projectPath(currentOrgSlug(), slug, `/events/${tab}`)

  // Reviewing a branch and fixing three of its events used to cost three round
  // trips through Settings > Branches, because closing the editor always landed
  // on the events list. Go back to wherever the editor was opened from instead
  // — react-router gives the initial history entry the key 'default', so this
  // only steps back when there is somewhere in-app to step back to, and the
  // list stays the answer for a cold-opened URL.
  const goBack = () => {
    if (location.key !== 'default') {
      navigate(-1)
      return
    }
    // With the query string: the list's filters and the `?branch=` EventsPage
    // carries here on purpose, which a cold-opened link otherwise lost (EVT-38).
    navigate(`${listPath}${location.search}`)
  }

  // A question raised while the event is being authored. It cannot be a comment
  // yet — a comment hangs off an event — so it is held here and posted the
  // moment one exists (tripl-htfn.1).
  const [draftNote, setDraftNote] = useState('')
  // What a previous attempt could not post, handed across the navigation below
  // so the words are not lost with the request that failed.
  const handoff = location.state as { commentDraft?: string; commentError?: string } | null

  const postDraftNote = async (created: EventMutationResponse): Promise<boolean> => {
    const body = draftNote.trim()
    if (!body) return true
    try {
      await eventCommentsApi.create(slug!, created.id, body, null)
      setDraftNote('')
      return true
    } catch (error) {
      // The event EXISTS. Leaving its author on a create form for it is the
      // worse failure — pressing Create again makes a second one — so land them
      // on the event, carrying what they wrote into its own composer. `replace`
      // keeps Back meaning what it meant before the save.
      navigate(projectPath(currentOrgSlug(), slug, `/events/${tab ?? 'all'}/${created.id}/edit${location.search}`), {
        replace: true,
        state: {
          commentDraft: body,
          commentError: `The event was created, but the note was not posted: ${getErrorMessage(error)}`,
        },
      })
      return false
    }
  }

  // Nothing is loaded for a viewer: they are sent on below.
  const eventTypesQuery = useQuery({
    queryKey: eventTypesKey(slug, branchId),
    queryFn: () => eventTypesApi.list(slug!, branchId),
    enabled: !!slug && canWrite,
  })
  const metaFieldsQuery = useQuery({
    queryKey: metaFieldsKey(slug, branchId),
    queryFn: () => metaFieldsApi.list(slug!, branchId),
    enabled: !!slug && canWrite,
  })
  const variablesQuery = useQuery({
    queryKey: variablesKey(slug, branchId),
    queryFn: () => variablesApi.list(slug!, branchId),
    enabled: !!slug && canWrite,
  })
  const eventQuery = useQuery({
    queryKey: eventKey(slug, branchId, eventId),
    queryFn: () => eventsApi.get(slug!, eventId!, branchId),
    enabled: !!slug && !!eventId && canWrite,
  })
  // The plan's branches, for two things: whether the event opened here lives
  // on the branch being edited (AU-1 / PL-2), and the branch's name in the
  // "added to branch" confirmation (JR-13). The same query the branch banner
  // runs, so it is read once.
  const rowBranchId = eventQuery.data?.branch_id
  const branchesQuery = useQuery({
    queryKey: planBranchesKey(slug),
    queryFn: () => planBranchesApi.list(slug!),
    enabled: !!slug && canWrite && (!!rowBranchId || (isNew && branchId !== null)),
    staleTime: 60_000,
  })

  // A viewer creating an event has nothing to be shown, so they are told so
  // once and returned to the list (AU-33). A toast id, so a re-run of the
  // effect does not stack a second one.
  useEffect(() => {
    if (!canWrite && isNew) toast.info('Only editors can add events.', { id: 'viewer-new-event' })
  }, [canWrite, isNew])

  // Viewers get the page built for reading — the event's detail page, with its
  // spec and activity — instead of a disabled form with live-looking
  // controls, required stars and authoring hints (#237 MT-28 / AU-33 / JR-18).
  if (!canWrite && slug) {
    return (
      <Navigate
        replace
        to={isNew ? `${listPath}${location.search}` : projectPath(currentOrgSlug(), slug, `/monitoring/event/${eventId}${location.search}`)}
      />
    )
  }

  // The event itself first: a missing one is not an error to retry but a
  // dead link, with the way back to the list (#237 SH-33).
  if (eventQuery.error) {
    return (
      <PageContainer width="narrow">
        <QueryErrorState
          error={eventQuery.error}
          title="Could not load this event"
          notFound={{ title: 'Event not found', back: { to: listPath, label: 'Back to Events' } }}
          onRetry={() => void eventQuery.refetch()}
        />
      </PageContainer>
    )
  }

  const loadError = eventTypesQuery.error ?? metaFieldsQuery.error ?? variablesQuery.error

  if (loadError) {
    return (
      <PageContainer width="narrow">
        {/* Names what failed, not the view (SH-33). */}
        <ErrorState
          title="Could not load the event types, meta fields or variables"
          error={loadError}
          onRetry={() => {
            void Promise.all([
              eventTypesQuery.refetch(),
              metaFieldsQuery.refetch(),
              variablesQuery.refetch(),
            ])
          }}
        />
      </PageContainer>
    )
  }

  const isLoading =
    eventTypesQuery.isLoading
    || metaFieldsQuery.isLoading
    || variablesQuery.isLoading
    || (!isNew && eventQuery.isLoading)
    // The branch lock below reads the branch list; rendering before it lands
    // showed an editable form with Save live until the list arrived (AU-1).
    || (!!rowBranchId && branchesQuery.isPending)

  // The form's shape while it loads, not a sentence in an empty column (AU-43).
  if (isLoading || !slug) {
    return (
      <PageContainer width="narrow">
        <PageSkeleton variant="form" label={isNew ? 'Loading the event form…' : 'Loading event…'} />
      </PageContainer>
    )
  }

  // A main event opened while a branch is active — or a branch row opened on
  // main — renders the row the lenient read found, but the save is strict and
  // answers "Event not found". Read-only, with the switch in Save's place.
  const branches = branchesQuery.data?.items
  const mainBranch = branches?.find(b => b.kind === 'main')
  const rowBranch = rowBranchId ? branches?.find(b => b.id === rowBranchId) : undefined
  // Without the list (it failed), a row read on a branch is still known to be
  // elsewhere whenever its id is not that branch's; on main the main branch's
  // id is unknown, so nothing is claimed there.
  const branchMismatch = branchesQuery.error
    ? !!eventId && !!rowBranchId && !!branchId && rowBranchId !== branchId
    : !!eventId && !!rowBranch && !!mainBranch && rowBranchId !== (branchId ?? mainBranch.id)
  const rowIsMain = rowBranch?.kind === 'main'
  const switchLink = rowBranch
    ? branchLink(projectPath(currentOrgSlug(), slug, `/events/${tab ?? 'all'}/${eventId}/edit`), rowIsMain ? null : rowBranch.id)
    : null
  const activeBranchName = branchId ? branches?.find(b => b.id === branchId)?.name : undefined

  // Say that it worked, and where (AU-21, JR-13): "Create event" used to step
  // back to a long list with nothing to find the new row by, and on a branch
  // the diff it had just grown was two clicks away.
  const announceCreated = (created: EventMutationResponse) => {
    if (branchId && activeBranchName) {
      toast.success(`Added ${created.name} to branch ${activeBranchName}`, {
        action: {
          label: 'View changes',
          onClick: () => navigate(projectPath(currentOrgSlug(), slug, `/branches/${branchId}`)),
        },
      })
      return
    }
    toast.success(`Created ${created.name}`, {
      action: {
        label: 'Open',
        onClick: () => navigate(branchLink(projectPath(currentOrgSlug(), slug, `/monitoring/event/${created.id}`), branchId).to),
      },
    })
  }
  const onCreated = async (created: EventMutationResponse): Promise<boolean> => {
    const closes = await postDraftNote(created)
    if (closes) {
      announceCreated(created)
      // The list scrolls to and marks the new row when it is next shown.
      rememberCreatedEvents(slug, [created.id])
    }
    return closes
  }

  const eventTypesData = eventTypesQuery.data ?? EMPTY_EVENT_TYPES
  // On a scoped event-type tab, pre-select that type when creating a new event.
  const defaultEventTypeId = isNew && tab && tab !== 'all'
    ? eventTypesData.find(et => et.name === tab)?.id
    : undefined

  return (
    <div className="h-full overflow-y-auto">
      <EventForm
        slug={slug}
        eventTypes={eventTypesData}
        metaFields={metaFieldsQuery.data ?? EMPTY_META_FIELDS}
        projectVariables={variablesQuery.data ?? EMPTY_VARIABLES}
        event={eventQuery.data ?? null}
        defaultEventTypeId={defaultEventTypeId}
        onClose={goBack}
        onCreated={onCreated}
        hasOtherUnsavedInput={draftNote.trim() !== ''}
        lockedReason={
          !branchMismatch
            ? undefined
            : rowBranch
              ? `This event lives on ${rowIsMain ? 'the main plan' : `branch ${rowBranch.name}`}, so it cannot be saved from here.`
              : 'This event lives outside the branch being edited, so it cannot be saved from here.'
        }
        lockedAction={
          branchMismatch && switchLink && rowBranch ? (
            <Button asChild>
              <Link to={switchLink.to} onClick={switchLink.onClick}>
                {rowIsMain ? 'Switch to main' : `Switch to ${rowBranch.name}`}
              </Link>
            </Button>
          ) : undefined
        }
        banner={
          // Only once the branch list is in: the banner reads the same query,
          // and mounting a second observer on a FAILED list refetches it, which
          // puts it back to pending — and the pending gate above then swapped
          // the page for its skeleton, unmounted the banner, failed again and
          // looped. Without the list the banner renders nothing anyway.
          eventId && branchesQuery.data ? (
            // The form is where a branch edit is actually made, and it was the
            // one authoring surface that never said which plan it was writing
            // to. The read is lenient and the write is strict, so a mismatch
            // rendered a perfectly normal form and failed as a bare 404 at
            // Save. Under the title, as part of the page, not above its back
            // link (AU-1).
            <EntityBranchBanner
              slug={slug}
              rowBranchId={eventQuery.data?.branch_id}
              path={projectPath(currentOrgSlug(), slug, `/events/${tab ?? 'all'}/${eventId}/edit`)}
              // Not this page's id on main: it is the branch row's, which main
              // would render again under a mismatch warning (EVT-42). The main
              // twin's page when the server names one, else the list on main.
              mainPath={
                eventQuery.data?.main_event_id
                  ? projectPath(currentOrgSlug(), slug, `/events/${tab ?? 'all'}/${eventQuery.data.main_event_id}/edit`)
                  : !tab || tab === 'all'
                    ? projectPath(currentOrgSlug(), slug, '/events')
                    : projectPath(currentOrgSlug(), slug, `/events/${tab}`)
              }
            />
          ) : undefined
        }
        beforeActions={
          eventId ? undefined : <DraftDiscussionNote value={draftNote} onChange={setDraftNote} />
        }
      />
      {/* The one home for the discussion, and outside the form on purpose: it
          is not plan content. Every other box on this page ships to whoever
          implements the event — Description is an indexed search column and a
          line in the pasted spec — so "should this fire on cancel too?" typed
          there reads as part of the specification. Edit only: there is no
          event to hang a thread on until one exists. */}
      {eventId && (
        // Below the sticky save bar with a clear break, so the page end is not
        // mistaken for more of the form (AU-6).
        <div className="mt-10 max-w-[880px] pb-10">
          {/* Health (F15, #268): what the event scores on the main plan and
              why. Renders nothing on a branch or for an archived event. */}
          <EventHealthCard slug={slug} eventId={eventId} className="mb-6" />
          {handoff?.commentError && (
            <p role="alert" className="mb-2 text-body-sm text-destructive">
              {handoff.commentError}
            </p>
          )}
          <CommentThread
            initialBody={handoff?.commentDraft}
            queryKey={eventCommentsKey(slug, eventId)}
            list={() => eventCommentsApi.list(slug, eventId)}
            create={(body, parentId) => eventCommentsApi.create(slug, eventId, body, parentId)}
            remove={commentId => eventCommentsApi.remove(slug, eventId, commentId)}
            onAction={(commentId, action, snoozedUntil) =>
              eventCommentsApi.action(slug, eventId, commentId, action, snoozedUntil)
            }
            authorName={comment => displayUser(usersById, comment.user_id)}
            heading="Discussion"
            emptyText="Nothing raised yet. Questions and notes here stay out of the spec."
            composerId="event-discussion-body"
            // @ offers the project's members; a mention notifies them (#259).
            mentionSlug={slug}
            // The form's card geometry (AU-8), not a smaller box of its own.
            className="flex flex-col rounded-card border bg-(--surface) p-4"
          />
        </div>
      )}
    </div>
  )
}
