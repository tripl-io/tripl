import { api } from './client'
import type {
  ConnectionSettings,
  DataSource,
  DataSourceDraftTestResult,
  DataSourceTestResult,
  DbType,
  JsonPathDiscovery,
} from '../types'

export interface DataSourceCreatePayload {
  name: string
  db_type: DbType
  host: string
  port: number
  database_name: string
  username?: string
  password?: string
  timeout_seconds?: number | null
  json_path_discovery?: JsonPathDiscovery | null
  connection_settings?: ConnectionSettings | null
}

export const dataSourcesApi = {
  list: () =>
    api.get<DataSource[]>('/data-sources'),

  get: (id: string) =>
    api.get<DataSource>(`/data-sources/${id}`),

  create: (data: DataSourceCreatePayload) => api.post<DataSource>('/data-sources', data),

  // connection_settings replaces the stored settings wholesale — a field left
  // out is cleared. The one exception is `sslkey`: omitting it keeps the stored
  // private key (like omitting the password); send '' to clear it.
  update: (id: string, data: {
    name?: string
    db_type?: DbType
    host?: string
    port?: number
    database_name?: string
    username?: string
    password?: string
    timeout_seconds?: number | null
    json_path_discovery?: JsonPathDiscovery | null
    connection_settings?: ConnectionSettings | null
  }) => api.patch<DataSource>(`/data-sources/${id}`, data),

  del: (id: string) =>
    api.del(`/data-sources/${id}`),

  testConnection: (id: string) =>
    api.post<DataSourceTestResult>(`/data-sources/${id}/test`, {}),

  /**
   * Test a config that has not been saved. Owner-only, like create;
   * nothing is stored, and every secret the probe needs is in this body.
   */
  testDraft: (data: Omit<DataSourceCreatePayload, 'name'> & { name?: string }) =>
    api.post<DataSourceDraftTestResult>('/data-sources/test', data),
}
