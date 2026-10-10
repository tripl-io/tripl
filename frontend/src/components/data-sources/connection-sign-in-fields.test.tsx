import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { DbType } from '@/types'
import { ConnectionCoreFields } from './connection-core-fields'
import { EMPTY_CONNECTION_CORE_FORM, type ConnectionCoreForm, type CoreMissing } from './connection-core'

function renderCore({
  dbType,
  mode = 'edit',
  value = EMPTY_CONNECTION_CORE_FORM,
  secretSet = false,
  secretError = null,
  missing = {},
  onChange = () => {},
  databricksAuth,
}: {
  dbType: DbType
  mode?: 'create' | 'edit'
  value?: ConnectionCoreForm
  secretSet?: boolean
  secretError?: string | null
  missing?: CoreMissing
  onChange?: (patch: Partial<ConnectionCoreForm>) => void
  databricksAuth?: 'pat' | 'oauth_m2m'
}) {
  render(
    <ConnectionCoreFields
      idPrefix="t"
      dbType={dbType}
      value={value}
      onChange={onChange}
      mode={mode}
      secretSet={secretSet}
      secretError={secretError}
      missing={missing}
      databricksAuth={databricksAuth}
    />,
  )
}

describe('the username a warehouse cannot sign in without', () => {
  it('is required for Trino, and offered a Trino-shaped example', () => {
    renderCore({ dbType: 'trino', mode: 'create', missing: { username: 'Required' } })
    const username = screen.getByLabelText('Username')
    expect(username).toBeRequired()
    expect(username).toHaveAttribute('placeholder', 'e.g. tripl')
    expect(username).toHaveAttribute('aria-invalid', 'true')
    expect(username).toHaveAccessibleDescription('Required')
  })

  it('stays optional for ClickHouse and PostgreSQL', () => {
    renderCore({ dbType: 'postgres', mode: 'create' })
    expect(screen.getByLabelText('Username')).not.toBeRequired()
  })

  it('is required as the Snowflake user', () => {
    renderCore({ dbType: 'snowflake', mode: 'create', missing: { username: 'Required' } })
    expect(screen.getByLabelText('User')).toBeRequired()
    expect(screen.getByLabelText('User')).toHaveAccessibleDescription('Required')
  })

  it('is required as the Athena access key ID', () => {
    renderCore({ dbType: 'athena', mode: 'create' })
    expect(screen.getByLabelText('Access key ID')).toBeRequired()
  })

  it('is required as the Databricks client ID only with OAuth', () => {
    renderCore({ dbType: 'databricks', mode: 'create', databricksAuth: 'oauth_m2m' })
    expect(screen.getByLabelText('OAuth client ID')).toBeRequired()
  })

  it('is not required as the Databricks client ID with an access token', () => {
    renderCore({ dbType: 'databricks', mode: 'create', databricksAuth: 'pat' })
    expect(screen.getByLabelText('OAuth client ID')).not.toBeRequired()
  })
})

describe('the stored password', () => {
  it('can be removed on edit, which also empties the password box', () => {
    const onChange = vi.fn()
    renderCore({ dbType: 'trino', secretSet: true, onChange })

    fireEvent.click(screen.getByLabelText('Remove the stored password'))

    expect(onChange).toHaveBeenCalledWith({ clearSecret: true, secret: '' })
  })

  it('locks the password box while its removal is asked for', () => {
    renderCore({
      dbType: 'clickhouse',
      secretSet: true,
      value: { ...EMPTY_CONNECTION_CORE_FORM, clearSecret: true },
    })
    expect(screen.getByLabelText('Password')).toBeDisabled()
    expect(screen.getByLabelText('Remove the stored password')).toBeChecked()
  })

  it('is not offered when none is stored', () => {
    renderCore({ dbType: 'trino', secretSet: false })
    expect(screen.queryByLabelText('Remove the stored password')).not.toBeInTheDocument()
  })

  it('is not offered on create', () => {
    renderCore({ dbType: 'trino', mode: 'create', secretSet: true })
    expect(screen.queryByLabelText('Remove the stored password')).not.toBeInTheDocument()
  })

  it('shows why a password cannot be saved under the password box', () => {
    renderCore({ dbType: 'trino', secretError: 'A password is only sent over HTTPS.' })
    const password = screen.getByLabelText('Password')
    expect(password).toHaveAttribute('aria-invalid', 'true')
    expect(password).toHaveAccessibleDescription('A password is only sent over HTTPS.')
  })
})
