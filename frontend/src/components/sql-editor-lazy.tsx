import { Suspense, type ComponentProps } from 'react'
import type * as SqlEditorModule from './sql-editor'
import { lazyWithReload } from '@/lib/lazyWithReload'

// CodeMirror and its SQL language are the bulk of the sql-editor chunk. The
// surfaces below only sometimes show an editor — a collapsed "Show SQL", the
// scan form's SQL step — so importing it statically made every monitoring
// drilldown and the Scans list pay for it up front (#194).
const SqlEditorImpl = lazyWithReload(() =>
  import('./sql-editor').then((module_) => ({ default: module_.SqlEditor })),
)

type SqlEditorProps = ComponentProps<typeof SqlEditorModule.SqlEditor>

/** {@link SqlEditorModule.SqlEditor}, loaded when it first renders. */
export function LazySqlEditor(props: SqlEditorProps) {
  return (
    <Suspense
      fallback={
        <div
          role="status"
          className="flex items-center justify-center rounded-md border text-body-sm text-fg-tertiary"
          style={{ minHeight: props.minHeight ?? '120px' }}
        >
          Loading editor…
        </div>
      }
    >
      <SqlEditorImpl {...props} />
    </Suspense>
  )
}
