import * as React from "react"
import * as DialogPrimitive from "@radix-ui/react-dialog"
import { X } from "lucide-react"
import { keepOpenForDemoGuide } from "@/components/ui/demo-guide-layer"
import { useUnsavedDialogGuard } from "@/hooks/useUnsavedChangesGuard"
import { DialogGuardContext, type DialogGuard } from "@/components/ui/dialog-guard"
import { cn } from "@/lib/utils"

/**
 * The dialog root. A form dialog says it holds unsaved input with `dirty`
 * (when the component rendering the dialog owns the form state) or with
 * `useDialogDirty` from ./dialog-guard (when a body inside it does). While either is true,
 * every close request — Escape, an outside click, the X and a
 * <DialogClose> Cancel — asks "Leave without saving?" first, and a reload or
 * tab close gets the browser's own prompt. One stray click outside used to
 * throw a typed form away with no word.
 *
 * The guard needs a controlled `open`: the parent closes the dialog from
 * `onOpenChange(false)`, which only arrives once the user has agreed. Closing
 * it by setting `open` (after a save) never asks.
 */
function Dialog({
  dirty = false,
  onOpenChange,
  children,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Root> & {
  /** The form in this dialog holds input that has not been saved. */
  dirty?: boolean
}) {
  const [dirtyParts, setDirtyParts] = React.useState<ReadonlySet<string>>(() => new Set())
  const { dialog: confirmDialog, requestClose } = useUnsavedDialogGuard(dirty || dirtyParts.size > 0)
  const report = React.useCallback((id: string, partDirty: boolean) => {
    setDirtyParts(prev => {
      if (prev.has(id) === partDirty) return prev
      const next = new Set(prev)
      if (partDirty) next.add(id)
      else next.delete(id)
      return next
    })
  }, [])
  const guard = React.useMemo<DialogGuard>(() => ({ report, requestLeave: requestClose }), [report, requestClose])
  const handleOpenChange = React.useCallback(
    (next: boolean) => {
      if (next) onOpenChange?.(true)
      else requestClose(() => onOpenChange?.(false))
    },
    [onOpenChange, requestClose],
  )
  return (
    <>
      {confirmDialog}
      <DialogPrimitive.Root onOpenChange={handleOpenChange} {...props}>
        <DialogGuardContext.Provider value={guard}>{children}</DialogGuardContext.Provider>
      </DialogPrimitive.Root>
    </>
  )
}

const DialogClose = DialogPrimitive.Close
const DialogPortal = DialogPrimitive.Portal

function DialogOverlay({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Overlay>) {
  return (
    <DialogPrimitive.Overlay
      data-slot="dialog-overlay"
      className={cn(
        "data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 fixed inset-0 z-(--z-modal) bg-black/50",
        className
      )}
      {...props}
    />
  )
}

function DialogContent({
  className,
  children,
  showCloseButton = true,
  onInteractOutside,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Content> & {
  showCloseButton?: boolean
}) {
  return (
    <DialogPortal>
      <DialogOverlay />
      <DialogPrimitive.Content
        data-slot="dialog-content"
        // The demo guide sits above dialogs and coaches the one that is open:
        // minimising it or hiding hints must not close that dialog.
        onInteractOutside={keepOpenForDemoGuide(onInteractOutside)}
        className={cn(
          // Inset 1rem from the screen edge with rounded corners on phones too;
          // full-bleed square dialogs put content against the glass.
          // A flex column, so a <DialogBody> child can take the scroll while
          // the header and footer stay put; without one the whole
          // content still scrolls as before. bg-popover: dialogs sit one step
          // up the elevation ladder, above the cards under them.
          "bg-popover text-popover-foreground data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95 fixed left-[50%] top-[50%] z-(--z-modal) flex flex-col w-[calc(100%-2rem)] max-w-lg max-h-[90vh] overflow-y-auto translate-x-[-50%] translate-y-[-50%] gap-4 rounded-card border p-6 shadow-lg duration-200",
          className
        )}
        {...props}
      >
        {children}
        {showCloseButton && (
          <DialogPrimitive.Close className="ring-offset-background focus:ring-ring data-[state=open]:bg-surface-hover data-[state=open]:text-fg-tertiary absolute right-4 top-4 flex size-8 items-center justify-center rounded-sm opacity-70 transition-opacity hover:opacity-100 focus:outline-none focus:ring-2 focus:ring-offset-2 disabled:pointer-events-none cursor-pointer">
            <X className="h-4 w-4" />
            <span className="sr-only">Close</span>
          </DialogPrimitive.Close>
        )}
      </DialogPrimitive.Content>
    </DialogPortal>
  )
}

// pr-8 keeps a long title from running under the close button (top-right,
// size-8), which every dialog had to remember at the call site before.
function DialogHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="dialog-header"
      className={cn("flex shrink-0 flex-col gap-2 pr-8 text-center sm:text-left", className)}
      {...props}
    />
  )
}

/**
 * The scrolling middle of a dialog. Put everything between
 * <DialogHeader> and <DialogFooter> in it and only this part scrolls, so the
 * title, the close button and Cancel/Save stay on screen in a long form. It
 * bleeds to the content's edges so the scrollbar sits at the dialog border.
 * When a <form> wraps header, body and footer, give the form
 * `flex min-h-0 flex-col gap-4` so the body can shrink.
 */
function DialogBody({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="dialog-body"
      className={cn("-mx-6 min-h-0 flex-1 overflow-y-auto px-6", className)}
      {...props}
    />
  )
}

function DialogFooter({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="dialog-footer"
      className={cn(
        "flex shrink-0 flex-col-reverse gap-2 sm:flex-row sm:justify-end",
        className
      )}
      {...props}
    />
  )
}

function DialogTitle({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Title>) {
  return (
    <DialogPrimitive.Title
      data-slot="dialog-title"
      className={cn("text-heading leading-none font-semibold", className)}
      {...props}
    />
  )
}

function DialogDescription({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Description>) {
  return (
    <DialogPrimitive.Description
      data-slot="dialog-description"
      className={cn("text-fg-secondary text-body", className)}
      {...props}
    />
  )
}

export {
  Dialog,
  DialogBody,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
}
