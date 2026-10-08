import { describe, expect, it } from 'vitest'
import type { DataSource } from '@/types'
import {
  EMPTY_CONNECTION_CORE_FORM,
  buildCoreCreatePayload,
  buildCoreUpdatePayload,
  connectionCoreMissing,
  coreConnectionChanged,
  dataSourceToCoreForm,
  serverCoreErrors,
  serviceAccountKeyError,
} from './connection-core'
import {
  EMPTY_CONNECTION_SETTINGS_FORM,
  buildConnectionSettings,
  connectionSettingsErrors,
  connectionSettingsToForm,
  httpPathError,
  pemError,
  snowflakeWarehouseError,
} from './connection-settings'

const CERT = '-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----'

describe('serviceAccountKeyError', () => {
  it('accepts an empty field and a service-account key', () => {
    expect(serviceAccountKeyError('')).toBeNull()
    expect(serviceAccountKeyError('  {"type":"service_account","private_key":"k"} ')).toBeNull()
  })

  it('rejects a partial paste, a non-object and another credential type', () => {
    expect(serviceAccountKeyError('{"type":"service_acc')).toMatch(/not valid JSON/)
    expect(serviceAccountKeyError('[1, 2]')).toMatch(/JSON object/)
    expect(serviceAccountKeyError('{"type":"authorized_user"}')).toMatch(/service_account/)
  })
})

describe('pemError', () => {
  it('accepts empty fields and complete PEM blocks, RSA and EC keys included', () => {
    expect(pemError('', 'certificate')).toBeNull()
    expect(pemError(CERT, 'certificate')).toBeNull()
    expect(
      pemError('-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----', 'private key'),
    ).toBeNull()
    expect(
      pemError('-----BEGIN EC PRIVATE KEY-----\nx\n-----END EC PRIVATE KEY-----', 'private key'),
    ).toBeNull()
  })

  // openssl pkcs12 / s_client -showcerts output carries text before the block;
  // libpq, OpenSSL and the backend all skip it, so the dialog must too.
  it('accepts a preamble before the block and TRUSTED / X509 certificate labels', () => {
    expect(
      pemError(`Bag Attributes\n    localKeyID: 01 02\nsubject=/CN=db\nissuer=/CN=ca\n${CERT}`, 'certificate'),
    ).toBeNull()
    expect(pemError(`# CA bundle\n${CERT}\n${CERT}`, 'certificate')).toBeNull()
    expect(
      pemError(
        '-----BEGIN TRUSTED CERTIFICATE-----\nMIIB\n-----END TRUSTED CERTIFICATE-----',
        'certificate',
      ),
    ).toBeNull()
    expect(
      pemError(
        '-----BEGIN X509 CERTIFICATE-----\nMIIB\n-----END X509 CERTIFICATE-----',
        'certificate',
      ),
    ).toBeNull()
    expect(
      pemError(
        'Bag Attributes\n    friendlyName: client\n-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----',
        'private key',
      ),
    ).toBeNull()
  })

  it('rejects an END line that comes before its BEGIN', () => {
    expect(
      pemError('-----END CERTIFICATE-----\n-----BEGIN CERTIFICATE-----\nMIIB', 'certificate'),
    ).toMatch(/incomplete/)
  })

  it('rejects a server path, a truncated block and the wrong kind of block', () => {
    expect(pemError('/etc/ssl/ca.pem', 'certificate')).toMatch(/BEGIN CERTIFICATE/)
    expect(pemError('-----BEGIN CERTIFICATE-----\nMIIB', 'certificate')).toMatch(/incomplete/)
    expect(pemError(CERT, 'private key')).toMatch(/BEGIN PRIVATE KEY/)
  })

  it('only checks PostgreSQL, and skips a key the operator asked to remove', () => {
    const bad = { ...EMPTY_CONNECTION_SETTINGS_FORM, sslrootcert: 'ca.pem', sslkey: 'key.pem' }
    expect(connectionSettingsErrors('clickhouse', bad)).toEqual({})
    expect(Object.keys(connectionSettingsErrors('postgres', bad)).sort()).toEqual([
      'sslkey',
      'sslrootcert',
    ])
    expect(
      connectionSettingsErrors('postgres', { ...bad, clearSslkey: true }),
    ).not.toHaveProperty('sslkey')
    // Greenplum and Redshift take the PostgreSQL settings, so they are checked too.
    for (const dbType of ['greenplum', 'redshift'] as const) {
      expect(Object.keys(connectionSettingsErrors(dbType, bad)).sort()).toEqual(['sslkey', 'sslrootcert'])
    }
  })
})

describe('coreConnectionChanged', () => {
  const source = {
    db_type: 'clickhouse',
    host: 'ch.example.com',
    port: 8123,
    database_name: 'analytics',
    username: 'reader',
    timeout_seconds: null,
    json_path_discovery: null,
  } as unknown as DataSource

  it('ignores the timeout and discovery mode, and notices host, port, user and secret', () => {
    const form = dataSourceToCoreForm(source)
    expect(coreConnectionChanged(source, { ...form, timeoutSeconds: '60' })).toBe(false)
    expect(coreConnectionChanged(source, { ...form, jsonPathDiscovery: 'all' })).toBe(false)
    expect(coreConnectionChanged(source, { ...form, host: 'ch2.example.com' })).toBe(true)
    expect(coreConnectionChanged(source, { ...form, port: 9000 })).toBe(true)
    expect(coreConnectionChanged(source, { ...form, username: 'admin' })).toBe(true)
    expect(coreConnectionChanged(source, { ...form, secret: 'rotated' })).toBe(true)
  })

  it('ignores port and username for BigQuery, which has neither', () => {
    const bq = { ...source, db_type: 'bigquery' } as DataSource
    const form = { ...EMPTY_CONNECTION_CORE_FORM, host: bq.host, databaseName: bq.database_name }
    expect(coreConnectionChanged(bq, { ...form, port: 1, username: 'x' })).toBe(false)
  })
})

describe('serverCoreErrors', () => {
  function apiError(fields: { loc: (string | number)[]; msg: string; type: string }[]) {
    return Object.assign(new Error('raw'), { fields })
  }

  it('maps named fields onto their controls and keeps the rest', () => {
    const result = serverCoreErrors(
      apiError([
        { loc: ['body', 'host'], msg: 'String should have at least 1 character', type: 'string_too_short' },
        { loc: ['body', 'database_name'], msg: 'Bad name', type: 'value_error' },
        { loc: ['body', 'timeout_seconds'], msg: 'Too large', type: 'less_than_equal' },
      ]),
      'clickhouse',
      'Required',
    )
    expect(result.fields).toEqual({ host: 'Required', databaseName: 'Bad name' })
    expect(result.rest).toBe('timeout_seconds: Too large')
  })

  it('only pins what the type shows: no port for BigQuery, no password box message elsewhere', () => {
    const port = { loc: ['body', 'port'], msg: 'Bad port', type: 'value_error' }
    const password = { loc: ['body', 'password'], msg: 'Bad key', type: 'value_error' }
    expect(serverCoreErrors(apiError([port, password]), 'bigquery', 'Required')).toEqual({
      fields: { secret: 'Bad key' },
      rest: 'port: Bad port',
    })
    expect(serverCoreErrors(apiError([port, password]), 'postgres', 'Required')).toEqual({
      fields: { port: 'Bad port' },
      rest: 'password: Bad key',
    })
  })

  it('returns an unstructured error whole, and nothing for no error', () => {
    expect(serverCoreErrors(new Error('Connection refused'), 'clickhouse', 'Required')).toEqual({
      fields: {},
      rest: 'Connection refused',
    })
    expect(serverCoreErrors(null, 'clickhouse', 'Required')).toEqual({ fields: {}, rest: null })
  })
})

describe('Databricks', () => {
  const form = {
    ...EMPTY_CONNECTION_SETTINGS_FORM,
    httpPath: ' /sql/1.0/warehouses/abc123 ',
    authType: 'oauth_m2m' as const,
    schemaName: 'analytics',
    schemaAllowlist: 'analytics, marts',
  }

  it('requires an HTTP path and refuses a whole URL', () => {
    expect(httpPathError('', 'Required')).toBe('Required')
    expect(httpPathError('https://dbc.cloud.databricks.com/sql/1.0/warehouses/x', 'Required')).toMatch(
      /only the path/,
    )
    expect(httpPathError('sql/1.0/warehouses/x', 'Required')).toMatch(/not an HTTP path/)
    expect(httpPathError('/sql/1.0/warehouses/abc123', 'Required')).toBeNull()
    expect(connectionSettingsErrors('databricks', EMPTY_CONNECTION_SETTINGS_FORM, 'Required')).toEqual({
      httpPath: 'Required',
    })
    expect(connectionSettingsErrors('databricks', form)).toEqual({})
    // Another warehouse never asks for one.
    expect(connectionSettingsErrors('clickhouse', EMPTY_CONNECTION_SETTINGS_FORM)).toEqual({})
  })

  it('sends only its own settings, trimmed, and reads them back', () => {
    const settings = buildConnectionSettings('databricks', form)
    expect(settings).toEqual({
      http_path: '/sql/1.0/warehouses/abc123',
      auth_type: 'oauth_m2m',
      schema_name: 'analytics',
      schema_allowlist: ['analytics', 'marts'],
    })
    const back = connectionSettingsToForm({
      location: null,
      maximum_bytes_billed: null,
      dataset_allowlist: null,
      sslmode: null,
      sslrootcert: null,
      sslcert: null,
      search_path: null,
      sslkey_set: false,
      http_path: '/sql/1.0/warehouses/abc123',
      auth_type: 'oauth_m2m',
      schema_name: 'analytics',
      schema_allowlist: ['analytics', 'marts'],
    })
    expect(back).toMatchObject({ ...form, httpPath: '/sql/1.0/warehouses/abc123' })
  })

  it('always connects on 443 and needs its token on create only', () => {
    const core = { ...EMPTY_CONNECTION_CORE_FORM, host: 'dbc.cloud.databricks.com', databaseName: 'main' }
    expect(buildCoreCreatePayload('databricks', core).port).toBe(443)
    expect(buildCoreUpdatePayload('databricks', { ...core, port: 8123 }).port).toBe(443)
    expect(connectionCoreMissing('databricks', { ...core, port: 0 }, 'create', 'Required')).toEqual({
      secret: 'Required',
    })
    expect(connectionCoreMissing('databricks', { ...core, port: 0 }, 'edit', 'Required')).toEqual({})
  })

  it('pins a refused token under its field, and never a port', () => {
    const apiError = (fields: { loc: (string | number)[]; msg: string; type: string }[]) =>
      Object.assign(new Error('raw'), { fields })
    const port = { loc: ['body', 'port'], msg: 'Bad port', type: 'value_error' }
    const password = { loc: ['body', 'password'], msg: 'Bad token', type: 'value_error' }
    expect(serverCoreErrors(apiError([port, password]), 'databricks', 'Required')).toEqual({
      fields: { secret: 'Bad token' },
      rest: 'port: Bad port',
    })
  })
})

describe('Snowflake', () => {
  const form = {
    ...EMPTY_CONNECTION_SETTINGS_FORM,
    warehouse: ' COMPUTE_WH ',
    role: 'ANALYST',
    snowflakeAuthType: 'key_pair' as const,
    schemaName: 'EVENTS',
    schemaAllowlist: 'EVENTS, MARTS',
  }

  it('requires a warehouse name', () => {
    expect(snowflakeWarehouseError('', 'Required')).toBe('Required')
    expect(snowflakeWarehouseError('WH; DROP', 'Required')).toMatch(/warehouse name only/)
    expect(snowflakeWarehouseError('COMPUTE_WH', 'Required')).toBeNull()
    expect(connectionSettingsErrors('snowflake', EMPTY_CONNECTION_SETTINGS_FORM, 'Required')).toEqual({
      warehouse: 'Required',
    })
    expect(connectionSettingsErrors('snowflake', form)).toEqual({})
  })

  it('sends only its own settings, trimmed, and reads them back', () => {
    expect(buildConnectionSettings('snowflake', form)).toEqual({
      warehouse: 'COMPUTE_WH',
      auth_type: 'key_pair',
      role: 'ANALYST',
      schema_name: 'EVENTS',
      schema_allowlist: ['EVENTS', 'MARTS'],
    })
    const back = connectionSettingsToForm({
      location: null,
      maximum_bytes_billed: null,
      dataset_allowlist: null,
      sslmode: null,
      sslrootcert: null,
      sslcert: null,
      search_path: null,
      sslkey_set: false,
      auth_type: 'key_pair',
      schema_name: 'EVENTS',
      schema_allowlist: ['EVENTS', 'MARTS'],
      warehouse: 'COMPUTE_WH',
      role: 'ANALYST',
    })
    // Snowflake's auth type never leaks into the Databricks select.
    expect(back).toMatchObject({ ...form, warehouse: 'COMPUTE_WH', authType: 'pat' })
  })

  it('always connects on 443 and needs its secret on create only', () => {
    const core = { ...EMPTY_CONNECTION_CORE_FORM, host: 'myorg-myaccount', databaseName: 'DB' }
    expect(buildCoreCreatePayload('snowflake', core).port).toBe(443)
    expect(buildCoreUpdatePayload('snowflake', { ...core, port: 8123 }).port).toBe(443)
    expect(connectionCoreMissing('snowflake', { ...core, port: 0 }, 'create', 'Required')).toEqual({
      secret: 'Required',
    })
    expect(connectionCoreMissing('snowflake', { ...core, port: 0 }, 'edit', 'Required')).toEqual({})
  })
})
