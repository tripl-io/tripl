import { EditorView, keymap, Prec, type Extension, type ViewUpdate } from '@uiw/react-codemirror'
import { findLinkTrigger, pickReplacement, triggerInCode, type LinkTrigger } from '@/lib/docLinkTrigger'

/**
 * CodeMirror side of the note editor's link picker (F24). The picker's state
 * lives in React ({@link useDocLinkPicker}); this reports the trigger under the
 * cursor after every edit, cursor move or focus change, and hands the picker
 * the navigation keys first — it answers false when closed, so the keys keep
 * their usual meaning.
 */
export interface LinkPickerBridge {
  onTrigger: (trigger: LinkTrigger | null, view: EditorView) => void
  onKey: (key: string) => boolean
}

const PICKER_KEYS = ['ArrowDown', 'ArrowUp', 'Enter', 'Tab', 'Escape'] as const

/**
 * Holds the editor's current callbacks. A plain object, not a React ref: the
 * extension is built once during render and only calls through it on events.
 */
export class LinkPickerBridgeBox {
  private bridge: LinkPickerBridge = { onTrigger: () => {}, onKey: () => false }

  set(bridge: LinkPickerBridge): void {
    this.bridge = bridge
  }

  get(): LinkPickerBridge {
    return this.bridge
  }
}

/** `bridge` is read on every event, so the editor may swap its callbacks freely. */
export function docLinkPickerExtension(bridge: LinkPickerBridgeBox): Extension {
  return [
    Prec.highest(keymap.of(PICKER_KEYS.map(key => ({ key, run: () => bridge.get().onKey(key) })))),
    EditorView.updateListener.of((update: ViewUpdate) => {
      if (!update.docChanged && !update.selectionSet && !update.focusChanged) return
      bridge.get().onTrigger(triggerAt(update.view), update.view)
    }),
  ]
}

/** The open `[[…` or `@…` right before a lone cursor in a focused editor. */
export function triggerAt(view: EditorView): LinkTrigger | null {
  const { state } = view
  const selection = state.selection.main
  if (!selection.empty || !view.hasFocus) return null
  const line = state.doc.lineAt(selection.head)
  const trigger = findLinkTrigger(state.sliceDoc(line.from, selection.head), line.from)
  if (!trigger) return null
  return triggerInCode(state.sliceDoc(0, selection.head), trigger) ? null : trigger
}

/** Write a picked reference over the trigger and put the cursor after it. */
export function insertPick(view: EditorView, trigger: LinkTrigger, insert: string): void {
  const { extra } = pickReplacement(trigger.mode, view.state.sliceDoc(trigger.to, trigger.to + 2))
  view.dispatch({
    changes: { from: trigger.from, to: trigger.to + extra, insert },
    selection: { anchor: trigger.from + insert.length },
    userEvent: 'input.complete',
    scrollIntoView: true,
  })
  view.focus()
}

/** Where the popup goes: just under the `[[` / `@`, in viewport pixels. */
export function measureTrigger(
  view: EditorView,
  trigger: LinkTrigger,
  write: (position: { left: number; top: number } | null) => void,
): void {
  view.requestMeasure({
    read: v => v.coordsAtPos(trigger.from),
    write: coords => write(coords ? { left: coords.left, top: coords.bottom + 4 } : null),
  })
}
