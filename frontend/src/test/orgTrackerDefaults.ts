import type { OrgTrackerDefaults } from '@/api/orgSettings'

/** Test fixture: an organization with a Jira site and account, no token, and a Linear key (F20 PR12). */
export function orgTrackerDefaultsFixture(overrides: Partial<OrgTrackerDefaults> = {}): OrgTrackerDefaults {
  return {
    organization: 'acme',
    jira: {
      base_url: 'https://acme.atlassian.net',
      auth_email: 'bot@acme.example',
      api_token_configured: true,
      project_key: '',
    },
    linear: { api_key_configured: true, team_id: 'ENG' },
    sources: {
      'jira.base_url': 'org',
      'jira.auth_email': 'org',
      'jira.api_token': 'org',
      'jira.project_key': 'default',
      'linear.api_key': 'org',
      'linear.team_id': 'org',
    },
    ...overrides,
  }
}
