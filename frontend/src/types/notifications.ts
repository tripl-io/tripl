/**
 * In-app notifications, subscriptions and per-person email preferences (#259).
 * Hand-written until the API types are regenerated.
 */

export type NotificationKind =
  | 'comment'
  | 'reply'
  | 'mention'
  | 'open_question'
  | 'signal'
  | 'branch_review_requested'
  | 'branch_approved'
  | 'branch_merged'
  | 'lifecycle'

export type SubscriptionEntityType = 'event' | 'event_type' | 'metric' | 'branch'
/** What a notification is about: a subscribable entity, or a note (a docs @mention). */
export type NotificationEntityType = SubscriptionEntityType | 'doc'

export type SubscriptionReason = 'author' | 'owner' | 'commenter' | 'reviewer' | 'manual'

export interface NotificationActor {
  id: string
  name: string
  email: string
}

/** One row of `GET /me/notifications`. */
export interface AppNotification {
  id: string
  project_id: string
  project_slug: string
  project_name: string
  kind: NotificationKind
  entity_type: NotificationEntityType
  entity_id: string
  title: string
  body: string
  /** In-app path the row opens. */
  url: string
  /** Who did it; null once their account is gone. */
  actor: NotificationActor | null
  read_at: string | null
  created_at: string
}

export interface NotificationPage {
  items: AppNotification[]
  next_cursor: string | null
}

export interface UnreadNotificationCount {
  unread: number
}

export interface MarkNotificationsReadResponse {
  updated: number
  unread: number
}

export type NotificationEmailMode = 'off' | 'instant' | 'daily' | 'weekly'

export interface NotificationPrefs {
  email_mode: NotificationEmailMode
  mentions_email: boolean
  /** False when the instance has no SMTP: the choice is kept, nothing is sent. */
  email_available: boolean
}

export type NotificationPrefsUpdate = Partial<Pick<NotificationPrefs, 'email_mode' | 'mentions_email'>>

/** `GET /projects/{slug}/subscriptions/{entity_type}/{entity_id}`: my state. */
export interface SubscriptionState {
  entity_type: SubscriptionEntityType
  /** The id the watch is kept under: a branch copy of an event resolves to its main twin. */
  entity_id: string
  watching: boolean
  muted: boolean
  reasons: SubscriptionReason[]
}
