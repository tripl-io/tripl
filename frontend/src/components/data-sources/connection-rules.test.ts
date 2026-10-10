import { describe, expect, it } from 'vitest'
import type { DataSource } from '@/types'
import {
  EMPTY_CONNECTION_CORE_FORM,
  TRINO_PASSWORD_OVER_HTTP,
  buildCoreUpdatePayload,
  connectionCoreMissing,
  connectionCoreSecretError,
  coreConnectionChanged,
  serverCoreErrors,
  usernameRequired,
} from './connection-core'
import {
  EMPTY_CONNECTION_SETTINGS_FORM,
  MAX_DATASET_ALLOWLIST,
  MAX_SCHEMA_ALLOWLIST,
  allowlistError,
  buildConnectionSettings,
  connectionSettingsErrors,
  editedSettingsErrors,
  qualifiedNameError,
} from './connection-settings'

const names = (count: number, prefix = 's') =>
  Array.from({ length: count }, (_, index) => `${prefix}${index}`).join(', ')

describe('username', () => {
  it('is required where the adapter refuses to sign in without one', () => {
    for (const dbType of ['trino', 'snowflake', 'athena'] as const) {
      expect(usernameRequired(dbType, 'pat')).toBe(true)
    }
    for (const dbType of ['clickhouse', 'postgres', 'bigquery'] as const) {
      expect(usernameRequired(dbType, 'pat')).toBe(false)
    }
    // Databricks reads it only as an OAuth client ID.
    expect(usernameRequired('databricks', 'pat')).toBe(false)
    expect(usernameRequired('databricks', 'oauth_m2m')).toBe(true)
  })

  it('is flagged inline when empty, on create and on edit', () => {
    const core = { ...EMPTY_CONNECTION_CORE_FORM, host: 'trino.internal', databaseName: 'hive' }
    expect(connectionCoreMissing('trino', core, 'create', 'Required')).toEqual({
      username: 'Required',
    })
    expect(connectionCoreMissing('trino', core, 'edit', 'Required')).toEqual({
      username: 'Required',
    })
    expect(connectionCoreMissing('trino', { ...core, username: ' tripl ' }, 'create', 'Required')).toEqual(
      {},
    )
    const dbx = { ...core, secret: 'dapi', host: 'dbc.cloud.databricks.com' }
    expect(connectionCoreMissing('databricks', dbx, 'create', 'Required', 'pat')).toEqual({})
    expect(connectionCoreMissing('databricks', dbx, 'create', 'Required', 'oauth_m2m')).toEqual({
      username: 'Required',
    })
  })
})

describe('Trino password over HTTP', () => {
  const core = { ...EMPTY_CONNECTION_CORE_FORM, host: 'trino.internal', databaseName: 'hive' }

  it('refuses a typed password with the HTTP scheme only', () => {
    const typed = { ...core, secret: 'hunter2' }
    expect(connectionCoreSecretError('trino', typed, { httpScheme: 'http', passwordSet: false })).toBe(
      TRINO_PASSWORD_OVER_HTTP,
    )
    expect(connectionCoreSecretError('trino', typed, { httpScheme: 'https', passwordSet: false })).toBeNull()
    expect(connectionCoreSecretError('trino', core, { httpScheme: 'http', passwordSet: false })).toBeNull()
    // Another warehouse has no scheme to check.
    expect(connectionCoreSecretError('postgres', typed, { httpScheme: 'http', passwordSet: false })).toBeNull()
  })

  it('counts a stored password on edit, until its removal is asked for', () => {
    const scheme = { httpScheme: 'http' as const, passwordSet: true }
    expect(connectionCoreSecretError('trino', core, scheme)).toBe(TRINO_PASSWORD_OVER_HTTP)
    expect(connectionCoreSecretError('trino', { ...core, clearSecret: true }, scheme)).toBeNull()
  })
})

describe('removing a stored password', () => {
  const source = {
    db_type: 'trino',
    host: 'trino.internal',
    port: 8080,
    database_name: 'hive',
    username: 'tripl',
  } as unknown as DataSource

  it('sends an empty password, and counts as a connection change', () => {
    const form = {
      ...EMPTY_CONNECTION_CORE_FORM,
      host: 'trino.internal',
      port: 8080,
      databaseName: 'hive',
      username: 'tripl',
    }
    expect(buildCoreUpdatePayload('trino', form)).not.toHaveProperty('password')
    expect(buildCoreUpdatePayload('trino', { ...form, clearSecret: true }).password).toBe('')
    // A typed password wins over the box.
    expect(buildCoreUpdatePayload('trino', { ...form, clearSecret: true, secret: 'new' }).password).toBe(
      'new',
    )
    expect(coreConnectionChanged(source, form)).toBe(false)
    expect(coreConnectionChanged(source, { ...form, clearSecret: true })).toBe(true)
  })
})

describe('server refusals', () => {
  const apiError = (fields: { loc: (string | number)[]; msg: string; type: string }[]) =>
    Object.assign(new Error('raw'), { fields })

  it('shows a connection settings refusal as the sentence it is', () => {
    const settings = {
      loc: ['body', 'connection_settings'],
      msg: "Invalid connection settings — schema_name 'hive.events' is not a valid name",
      type: 'connection_settings',
    }
    expect(serverCoreErrors(apiError([settings]), 'trino', 'Required')).toEqual({
      fields: {},
      rest: "Invalid connection settings — schema_name 'hive.events' is not a valid name",
    })
  })

  it('pins a sign-in refusal under the username and password boxes', () => {
    const username = { loc: ['body', 'username'], msg: 'Trino needs a user', type: 'missing' }
    const password = { loc: ['body', 'password'], msg: 'Only over HTTPS', type: 'value_error' }
    expect(serverCoreErrors(apiError([username, password]), 'trino', 'Required')).toEqual({
      fields: { username: 'Required', secret: 'Only over HTTPS' },
      rest: null,
    })
  })
})

describe('schema names and allowlists', () => {
  it('names a qualified name and what to type instead', () => {
    expect(qualifiedNameError('')).toBeNull()
    expect(qualifiedNameError('events')).toBeNull()
    expect(qualifiedNameError(' hive.events ')).toBe('Use the name alone, like events, not hive.events.')
    expect(qualifiedNameError('hive.')).toBe('Use the name alone, without a dot.')
  })

  it('caps an allowlist at its distinct entries, as the server does', () => {
    expect(allowlistError(names(MAX_SCHEMA_ALLOWLIST), MAX_SCHEMA_ALLOWLIST, ['schema', 'schemas'])).toBeNull()
    expect(
      allowlistError(`${names(MAX_SCHEMA_ALLOWLIST)}, s0`, MAX_SCHEMA_ALLOWLIST, ['schema', 'schemas']),
    ).toBeNull()
    expect(
      allowlistError(names(MAX_SCHEMA_ALLOWLIST + 1), MAX_SCHEMA_ALLOWLIST, ['schema', 'schemas']),
    ).toMatch(/^At most 50 schemas: this lists 51\.$/)
    expect(allowlistError('events, main.marts', MAX_SCHEMA_ALLOWLIST, ['schema', 'schemas'])).toBe(
      'Use the name alone, like marts, not main.marts.',
    )
  })

  it('checks the scope of every warehouse that has one', () => {
    const qualified = {
      ...EMPTY_CONNECTION_SETTINGS_FORM,
      httpPath: '/sql/1.0/warehouses/x',
      warehouse: 'WH',
      schemaName: 'main.analytics',
      schemaAllowlist: names(MAX_SCHEMA_ALLOWLIST + 1),
      datasetAllowlist: 'my-project.analytics',
    }
    for (const dbType of ['databricks', 'snowflake', 'trino'] as const) {
      expect(Object.keys(connectionSettingsErrors(dbType, qualified)).sort()).toEqual([
        'schemaAllowlist',
        'schemaName',
      ])
    }
    // Athena has no default schema, and lists Glue databases.
    expect(connectionSettingsErrors('athena', qualified)).toEqual({
      schemaAllowlist: `At most ${MAX_SCHEMA_ALLOWLIST} databases: this lists ${MAX_SCHEMA_ALLOWLIST + 1}.`,
    })
    expect(connectionSettingsErrors('bigquery', qualified)).toEqual({
      datasetAllowlist: 'Use the name alone, like analytics, not my-project.analytics.',
    })
    expect(
      connectionSettingsErrors('bigquery', {
        ...EMPTY_CONNECTION_SETTINGS_FORM,
        datasetAllowlist: names(MAX_DATASET_ALLOWLIST + 1, 'ds'),
      }),
    ).toHaveProperty('datasetAllowlist')
    expect(connectionSettingsErrors('clickhouse', qualified)).toEqual({})
  })

  it('checks only what changed from the stored settings, but always a required one', () => {
    const stored = { ...EMPTY_CONNECTION_SETTINGS_FORM, schemaName: 'main.analytics' }
    expect(editedSettingsErrors('trino', stored, stored)).toEqual({})
    expect(editedSettingsErrors('trino', { ...stored, schemaName: 'a.b' }, stored)).toHaveProperty(
      'schemaName',
    )
    // An Athena result location used to be checked and then dropped.
    expect(
      editedSettingsErrors('athena', { ...stored, s3OutputLocation: 'bucket/x' }, stored),
    ).toHaveProperty('s3OutputLocation')
    expect(editedSettingsErrors('databricks', stored, stored)).toEqual({ httpPath: 'Required' })
  })

  it('sends each allowlist as names, or null for none', () => {
    const form = { ...EMPTY_CONNECTION_SETTINGS_FORM, schemaAllowlist: ' a, b  c ', datasetAllowlist: '' }
    expect(buildConnectionSettings('trino', form)).toMatchObject({ schema_allowlist: ['a', 'b', 'c'] })
    expect(buildConnectionSettings('athena', form)).toMatchObject({ schema_allowlist: ['a', 'b', 'c'] })
    expect(buildConnectionSettings('bigquery', form)).toMatchObject({ dataset_allowlist: null })
  })
})
