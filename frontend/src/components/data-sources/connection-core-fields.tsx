import type { DbType, JsonPathDiscovery } from '@/types'
import { Upload } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Chip } from '@/components/primitives/chip'
import { useId, useRef, useState, type ChangeEvent, type ReactNode } from 'react'
import { FieldError } from '@/components/forms/FieldError'
import { examplePlaceholder } from '@/components/forms/placeholders'
import { invalidAria } from '@/components/forms/validation'
import {
  ERROR_CLASS,
  FIELD_COL_CLASS,
  HELP_CLASS,
  PASSWORD_INPUT_PROPS,
  SECRET_INPUT_PROPS,
  SELECT_CLASS,
  TEXTAREA_CLASS,
} from './connection-settings'
import type { ConnectionCoreForm, CoreMissing } from './connection-core'

// ClickHouse JSON path discovery options (the preview step that enumerates
// candidate JSON paths). Defaults to "dynamic" — the effective backend default
// when the stored value is null.
const JSON_PATH_DISCOVERY_OPTIONS: { value: JsonPathDiscovery; label: string }[] = [
  { value: 'dynamic', label: 'Dynamic (recommended)' },
  { value: 'all', label: 'All paths' },
]

const JSON_PATH_DISCOVERY_HELP =
  'Dynamic lists only the important typed JSON sub-paths (faster). ' +
  'All lists every path including rarely-used ones (slower, exhaustive).'

// Every warehouse honours the timeout — including BigQuery, which used to get no
// deadline at all. The field is therefore shown for all of them; the placeholder
// stands for the server-side default (300s).
const TIMEOUT_HELP =
  'Connect and query budget. A query that outruns it is cancelled instead of holding a worker. ' +
  'Empty means the 300s default.'

interface ConnectionCoreFieldsProps {
  idPrefix: string
  dbType: DbType
  value: ConnectionCoreForm
  onChange: (patch: Partial<ConnectionCoreForm>) => void
  /**
   * 'create' requires a secret up front. 'edit' never pre-fills one (the API does
   * not return it) and keeps the stored secret when the field is left empty.
   */
  mode: 'create' | 'edit'
  /** True when the source already stores a password / service-account key. */
  secretSet?: boolean
  /** Why the typed secret cannot be saved (a malformed key file), shown inline. */
  secretError?: string | null
  /**
   * Required fields left empty on the last submit or test, from
   * `connectionCoreMissing`. The inputs carry `aria-required` rather than
   * `required`: the dialogs are `noValidate` and flag every empty field inline.
   */
  missing?: CoreMissing
}

/**
 * The core connection controls that apply to `dbType`, and nothing else — the
 * single implementation shared by the create and the edit dialog, so the two can
 * no longer drift apart.
 *
 * BigQuery has no host, no port and no username: it is a project id, a default
 * dataset and a service-account key. Showing it a "Port" box (which the adapter
 * deletes) or cramming a JSON key into a one-line password input was the bug
 * this component exists to prevent.
 */
export function ConnectionCoreFields({
  idPrefix,
  dbType,
  value,
  onChange,
  mode,
  secretSet = false,
  secretError = null,
  missing = {},
}: ConnectionCoreFieldsProps) {
  const isEdit = mode === 'edit'
  const secretErrorId = useId()
  const secretName =
    dbType === 'bigquery'
      ? 'Service account key'
      : dbType === 'databricks'
        ? 'Access token or OAuth secret'
        : dbType === 'snowflake'
          ? 'Password or private key'
          : 'Password'
  const keyError = missing.secret ?? secretError

  // Three states, three different sentences.
  //
  // `&& secretSet`, like the BigQuery key above: on a source with no stored
  // password the field said "Leave empty to keep" directly above a hint reading
  // "Password: not set.". Falling back to masked dots for that case
  // only inverted the contradiction — eight dots in the same grey as the
  // "default" placeholder next to it read as an 8-character stored password,
  // still directly above "Password: not set.". The empty state now
  // says it is empty, the way the instance SMTP password field already does
  // ("Not configured").
  // On create the box is labelled by what it wants, not by eight dots that
  // read as a password already typed in.
  const passwordPlaceholder = !isEdit
    ? 'Password'
    : secretSet
      ? 'Leave empty to keep'
      : 'No password stored'

  // On edit the secret is write-only: we can say whether one is stored, never
  // what it is. An empty field therefore means "keep what is stored".
  const secretStatus = isEdit ? (
    <p className={HELP_CLASS}>
      {secretSet
        ? `${secretName}: set. Leave empty to keep it — it is never sent back to the browser.`
        : `${secretName}: not set.`}
    </p>
  ) : null

  return (
    <>
      {dbType === 'bigquery' ? (
        <>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className={FIELD_COL_CLASS}>
              <Label htmlFor={`${idPrefix}-project-id`}>Project ID</Label>
              <Input
                id={`${idPrefix}-project-id`}
                value={value.host}
                onChange={(e) => onChange({ host: e.target.value })}
                aria-required
                placeholder={examplePlaceholder('my-gcp-project')}
                {...invalidAria(`${idPrefix}-project-id`, missing.host)}
              />
              <FieldError inputId={`${idPrefix}-project-id`} message={missing.host} />
              <p className={HELP_CLASS}>
                The GCP project the queries run in and that gets billed for the bytes they scan.
              </p>
            </div>
            <div className={FIELD_COL_CLASS}>
              <Label htmlFor={`${idPrefix}-default-dataset`}>Default dataset</Label>
              <Input
                id={`${idPrefix}-default-dataset`}
                value={value.databaseName}
                onChange={(e) => onChange({ databaseName: e.target.value })}
                aria-required
                placeholder={examplePlaceholder('analytics')}
                {...invalidAria(`${idPrefix}-default-dataset`, missing.databaseName)}
              />
              <FieldError inputId={`${idPrefix}-default-dataset`} message={missing.databaseName} />
              <p className={HELP_CLASS}>
                Where unqualified table names resolve. Anything else must be in the dataset
                allowlist below.
              </p>
            </div>
          </div>
          <div className={FIELD_COL_CLASS}>
            <Label htmlFor={`${idPrefix}-service-account-json`}>Service account JSON</Label>
            <textarea
              id={`${idPrefix}-service-account-json`}
              value={value.secret}
              onChange={(e) => onChange({ secret: e.target.value })}
              aria-required={!isEdit || undefined}
              rows={6}
              placeholder={
                isEdit && secretSet
                  ? 'A key is stored. Leave empty to keep it.'
                  : 'Paste the key file’s JSON, or upload the file below'
              }
              className={TEXTAREA_CLASS}
              aria-invalid={keyError ? true : undefined}
              aria-describedby={keyError ? secretErrorId : undefined}
              {...SECRET_INPUT_PROPS}
            />
            <KeyFileInput
              id={`${idPrefix}-service-account-file`}
              onLoad={(text) => onChange({ secret: text })}
            />
            {keyError && (
              <p id={secretErrorId} role="alert" className={ERROR_CLASS}>
                {keyError}
              </p>
            )}
            {secretStatus ?? (
              <p className={HELP_CLASS}>
                The whole key file. Stored encrypted and never shown again.
              </p>
            )}
          </div>
        </>
      ) : dbType === 'databricks' ? (
        <DatabricksCoreFields
          idPrefix={idPrefix}
          value={value}
          onChange={onChange}
          isEdit={isEdit}
          secretSet={secretSet}
          secretStatus={secretStatus}
          missing={missing}
        />
      ) : dbType === 'snowflake' ? (
        <SnowflakeCoreFields
          idPrefix={idPrefix}
          value={value}
          onChange={onChange}
          isEdit={isEdit}
          secretSet={secretSet}
          secretStatus={secretStatus}
          missing={missing}
        />
      ) : (
        <>
          {/* One column on phones: in a 375px dialog five columns left Port
              about 50px wide. */}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-5">
            <div className={`sm:col-span-2 ${FIELD_COL_CLASS}`}>
              <Label htmlFor={`${idPrefix}-host`}>Host</Label>
              <Input
                id={`${idPrefix}-host`}
                value={value.host}
                onChange={(e) => onChange({ host: e.target.value })}
                aria-required
                placeholder={examplePlaceholder('clickhouse.internal')}
                {...invalidAria(`${idPrefix}-host`, missing.host)}
              />
              <FieldError inputId={`${idPrefix}-host`} message={missing.host} />
            </div>
            <div className={FIELD_COL_CLASS}>
              <Label htmlFor={`${idPrefix}-port`}>Port</Label>
              <Input
                id={`${idPrefix}-port`}
                type="number"
                value={value.port}
                onChange={(e) => onChange({ port: Number(e.target.value) })}
                aria-required
                {...invalidAria(`${idPrefix}-port`, missing.port)}
              />
              <FieldError inputId={`${idPrefix}-port`} message={missing.port} />
            </div>
            <div className={`sm:col-span-2 ${FIELD_COL_CLASS}`}>
              <Label htmlFor={`${idPrefix}-database`}>Database</Label>
              <Input
                id={`${idPrefix}-database`}
                value={value.databaseName}
                onChange={(e) => onChange({ databaseName: e.target.value })}
                aria-required
                placeholder={examplePlaceholder('analytics')}
                {...invalidAria(`${idPrefix}-database`, missing.databaseName)}
              />
              <FieldError inputId={`${idPrefix}-database`} message={missing.databaseName} />
            </div>
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className={FIELD_COL_CLASS}>
              <Label htmlFor={`${idPrefix}-username`}>Username</Label>
              <Input
                id={`${idPrefix}-username`}
                value={value.username}
                onChange={(e) => onChange({ username: e.target.value })}
                placeholder={examplePlaceholder('default')}
                {...SECRET_INPUT_PROPS}
              />
            </div>
            <div className={FIELD_COL_CLASS}>
              <Label htmlFor={`${idPrefix}-password`}>Password</Label>
              <Input
                id={`${idPrefix}-password`}
                type="password"
                value={value.secret}
                onChange={(e) => onChange({ secret: e.target.value })}
                placeholder={passwordPlaceholder}
                {...PASSWORD_INPUT_PROPS}
              />
              {secretStatus}
            </div>
          </div>
        </>
      )}

      {/* Applies to every warehouse — BigQuery included. Full-width column with
          the number box held narrow: as the lone child of a `grid-cols-2` row the
          help wrapped into four ragged lines down the left half while the right
          half of the dialog stayed empty. */}
      <div className={FIELD_COL_CLASS}>
        {/* The unit in words, like the scan form's "Lookback (hours)". */}
        <Label htmlFor={`${idPrefix}-timeout`}>Timeout (seconds)</Label>
        <Input
          id={`${idPrefix}-timeout`}
          type="number"
          min={1}
          step={1}
          value={value.timeoutSeconds}
          onChange={(e) => onChange({ timeoutSeconds: e.target.value })}
          placeholder="Default"
          className="max-w-[10rem]"
        />
        <p className={HELP_CLASS}>{TIMEOUT_HELP}</p>
      </div>

      {dbType === 'clickhouse' && (
        <div className={FIELD_COL_CLASS}>
          <Label htmlFor={`${idPrefix}-json-path-discovery`}>JSON path discovery</Label>
          <select
            id={`${idPrefix}-json-path-discovery`}
            value={value.jsonPathDiscovery}
            onChange={(e) => onChange({ jsonPathDiscovery: e.target.value as JsonPathDiscovery })}
            className={SELECT_CLASS}
          >
            {JSON_PATH_DISCOVERY_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
          <p className={HELP_CLASS}>{JSON_PATH_DISCOVERY_HELP}</p>
        </div>
      )}
    </>
  )
}

interface DatabricksCoreFieldsProps {
  idPrefix: string
  value: ConnectionCoreForm
  onChange: (patch: Partial<ConnectionCoreForm>) => void
  isEdit: boolean
  secretSet: boolean
  secretStatus: ReactNode
  missing: CoreMissing
}

/**
 * Databricks: a workspace hostname, a catalog, and a token. The port is always
 * 443 (HTTPS) and is not shown. The username box carries a service principal's
 * OAuth client ID and is only read when Authentication below is OAuth M2M; with a
 * personal access token it stays empty.
 */
function DatabricksCoreFields({
  idPrefix,
  value,
  onChange,
  isEdit,
  secretSet,
  secretStatus,
  missing,
}: DatabricksCoreFieldsProps) {
  return (
    <>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className={FIELD_COL_CLASS}>
          <Label htmlFor={`${idPrefix}-server-hostname`}>Server hostname</Label>
          <Input
            id={`${idPrefix}-server-hostname`}
            value={value.host}
            onChange={(e) => onChange({ host: e.target.value })}
            aria-required
            placeholder={examplePlaceholder('dbc-a1b2c3d4-e5f6.cloud.databricks.com')}
            {...invalidAria(`${idPrefix}-server-hostname`, missing.host)}
          />
          <FieldError inputId={`${idPrefix}-server-hostname`} message={missing.host} />
          <p className={HELP_CLASS}>
            From the SQL warehouse’s Connection details tab, without https://.
          </p>
        </div>
        <div className={FIELD_COL_CLASS}>
          <Label htmlFor={`${idPrefix}-catalog`}>Catalog</Label>
          <Input
            id={`${idPrefix}-catalog`}
            value={value.databaseName}
            onChange={(e) => onChange({ databaseName: e.target.value })}
            aria-required
            placeholder={examplePlaceholder('main')}
            {...invalidAria(`${idPrefix}-catalog`, missing.databaseName)}
          />
          <FieldError inputId={`${idPrefix}-catalog`} message={missing.databaseName} />
          <p className={HELP_CLASS}>The Unity Catalog catalog that queries and browsing use.</p>
        </div>
      </div>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className={FIELD_COL_CLASS}>
          <Label htmlFor={`${idPrefix}-client-id`}>OAuth client ID</Label>
          <Input
            id={`${idPrefix}-client-id`}
            value={value.username}
            onChange={(e) => onChange({ username: e.target.value })}
            placeholder="Service principal only"
            {...SECRET_INPUT_PROPS}
          />
          <p className={HELP_CLASS}>
            Only for OAuth machine-to-machine. Leave empty with an access token.
          </p>
        </div>
        <div className={FIELD_COL_CLASS}>
          <Label htmlFor={`${idPrefix}-token`}>Access token or OAuth secret</Label>
          <Input
            id={`${idPrefix}-token`}
            type="password"
            value={value.secret}
            onChange={(e) => onChange({ secret: e.target.value })}
            aria-required={!isEdit || undefined}
            placeholder={
              !isEdit ? 'dapi…' : secretSet ? 'Leave empty to keep' : 'No token stored'
            }
            {...invalidAria(`${idPrefix}-token`, missing.secret)}
            {...PASSWORD_INPUT_PROPS}
          />
          <FieldError inputId={`${idPrefix}-token`} message={missing.secret} />
          {secretStatus ?? (
            <p className={HELP_CLASS}>Stored encrypted and never shown again.</p>
          )}
        </div>
      </div>
    </>
  )
}

/**
 * Snowflake: an account identifier, a database, a user and its password or
 * private key. The port is always 443 (HTTPS) and is not shown; whether the
 * secret is a password or a key is the Authentication setting below.
 */
function SnowflakeCoreFields({
  idPrefix,
  value,
  onChange,
  isEdit,
  secretSet,
  secretStatus,
  missing,
}: DatabricksCoreFieldsProps) {
  return (
    <>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className={FIELD_COL_CLASS}>
          <Label htmlFor={`${idPrefix}-account`}>Account identifier</Label>
          <Input
            id={`${idPrefix}-account`}
            value={value.host}
            onChange={(e) => onChange({ host: e.target.value })}
            aria-required
            placeholder={examplePlaceholder('myorg-myaccount')}
            {...invalidAria(`${idPrefix}-account`, missing.host)}
          />
          <FieldError inputId={`${idPrefix}-account`} message={missing.host} />
          <p className={HELP_CLASS}>
            Organization and account name (or the account locator with its region), from
            Account details in Snowsight.
          </p>
        </div>
        <div className={FIELD_COL_CLASS}>
          <Label htmlFor={`${idPrefix}-sf-database`}>Database</Label>
          <Input
            id={`${idPrefix}-sf-database`}
            value={value.databaseName}
            onChange={(e) => onChange({ databaseName: e.target.value })}
            aria-required
            placeholder={examplePlaceholder('ANALYTICS')}
            {...invalidAria(`${idPrefix}-sf-database`, missing.databaseName)}
          />
          <FieldError inputId={`${idPrefix}-sf-database`} message={missing.databaseName} />
          <p className={HELP_CLASS}>The database that queries and browsing use.</p>
        </div>
      </div>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className={FIELD_COL_CLASS}>
          <Label htmlFor={`${idPrefix}-sf-user`}>User</Label>
          <Input
            id={`${idPrefix}-sf-user`}
            value={value.username}
            onChange={(e) => onChange({ username: e.target.value })}
            placeholder={examplePlaceholder('TRIPL_READER')}
            {...SECRET_INPUT_PROPS}
          />
        </div>
        <div className={FIELD_COL_CLASS}>
          <Label htmlFor={`${idPrefix}-sf-secret`}>Password or private key</Label>
          <Input
            id={`${idPrefix}-sf-secret`}
            type="password"
            value={value.secret}
            onChange={(e) => onChange({ secret: e.target.value })}
            aria-required={!isEdit || undefined}
            placeholder={
              !isEdit ? 'Password' : secretSet ? 'Leave empty to keep' : 'No secret stored'
            }
            {...invalidAria(`${idPrefix}-sf-secret`, missing.secret)}
            {...PASSWORD_INPUT_PROPS}
          />
          <FieldError inputId={`${idPrefix}-sf-secret`} message={missing.secret} />
          {secretStatus ?? (
            <p className={HELP_CLASS}>
              For key-pair sign-in, paste the unencrypted PKCS#8 PEM key. Stored encrypted and
              never shown again.
            </p>
          )}
        </div>
      </div>
    </>
  )
}

/**
 * "Upload key file": reads a downloaded service-account JSON into the field, so
 * the key does not have to travel through the clipboard. The file
 * never leaves the browser until the form is saved.
 *
 * The native file input is visually hidden behind an outline button:
 * the browser's "Choose File / No file chosen" control clashed with the rest
 * of the dialog, most of all in dark mode. The loaded file's name shows as a
 * chip beside the button instead.
 */
function KeyFileInput({ id, onLoad }: { id: string; onLoad: (text: string) => void }) {
  // A read can fail (the file was moved after it was picked, or a permission
  // or IO error); that used to be an unhandled rejection with no feedback.
  const [loadError, setLoadError] = useState<string | null>(null)
  const [fileName, setFileName] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const errorId = `${id}-error`
  const handleChange = (e: ChangeEvent<HTMLInputElement>) => {
    const input = e.currentTarget
    const file = input.files?.[0]
    if (!file) return
    setLoadError(null)
    file.text().then(
      (text) => {
        setFileName(file.name)
        onLoad(text)
      },
      () => {
        setFileName(null)
        setLoadError('Could not read that file. Pick it again.')
      },
    )
    // Let the same file be picked again after an edit of the textarea.
    input.value = ''
  }
  return (
    <div className="grid gap-1">
      <div className="flex flex-wrap items-center gap-2">
        {/* The button is the pointer and keyboard target. The input stays in
            the DOM with a name (for assistive tech and tests) but out of the
            tab order, so focus does not stop twice on one action. */}
        <input
          ref={inputRef}
          id={id}
          type="file"
          accept="application/json,.json"
          onChange={handleChange}
          aria-label="Service account key file"
          aria-invalid={loadError ? true : undefined}
          aria-describedby={loadError ? errorId : undefined}
          tabIndex={-1}
          className="sr-only"
        />
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => inputRef.current?.click()}
          aria-invalid={loadError ? true : undefined}
          aria-describedby={loadError ? errorId : undefined}
        >
          <Upload aria-hidden="true" />
          Upload key file…
        </Button>
        {fileName && (
          <Chip size="xs" title={fileName} className="max-w-60 truncate">
            {fileName}
          </Chip>
        )}
      </div>
      {loadError && (
        <p id={errorId} role="alert" className={ERROR_CLASS}>
          {loadError}
        </p>
      )}
    </div>
  )
}
