import type { BindingExample } from './bindingExample'

/**
 * The one sentence that separates the two dotted things on this screen.
 *
 * A reader asked whether the path under Data bindings and the token offered
 * after `$` in a field value are the same. They are not — a binding is a
 * warehouse address, a token is a variable's NAME — and they look alike because
 * a scan that discovers a path stores it as the binding and, when every short
 * name is taken, as the name too. Showing the project's own pair says that
 * faster than explaining it (tripl-htfn.3).
 */
export function BindingVersusTokenNote({ example }: { example: BindingExample }) {
  return (
    <p className="text-caption text-fg-tertiary">
      A binding is where the value lives in the warehouse. It is not what you type in a field
      value — that is the property&apos;s name.{' '}
      {example.fromProject ? 'In this project, for instance, scans read' : 'For instance, scans read'}{' '}
      <code className="rounded-sm bg-muted px-1">{example.binding}</code> and you write{' '}
      <code className="rounded-sm bg-muted px-1">{'${' + example.name + '}'}</code>.
    </p>
  )
}
