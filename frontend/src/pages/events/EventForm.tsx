/**
 * The single-event authoring page.
 *
 * This module is the route's entry (App.tsx lazy-loads its default export) and
 * the form's public name. It used to hold all of it in 1,600 lines — five
 * helper components, twenty-odd pieces of state, eight queries and the route
 * page. The parts now live beside it:
 *
 *   EventEditPage.tsx       the route: loading, the branch banner, discussion
 *   EventFormView.tsx       the form: state, the save, the Details card
 *   EventFormCards.tsx      Tags & breakdowns, Field values, Meta fields
 *   eventFormFields.tsx     one field's control and the notes under it
 *   SuccessorPicker.tsx     "Replaced by" on a deprecated event
 *   useEventIdentityProbe   the duplicate-identity check
 *   eventFormValues.ts      pure value handling (sunset zone, chips, carry-over)
 */
import EventEditPage from './EventEditPage'

export { EventForm } from './EventFormView'
export default EventEditPage
