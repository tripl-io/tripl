import { Toaster as Sonner } from "sonner"
import { useTheme } from "@/components/theme-provider"

type ToasterProps = React.ComponentProps<typeof Sonner>

function Toaster({ ...props }: ToasterProps) {
  const { theme = "system" } = useTheme()

  return (
    <Sonner
      theme={theme as ToasterProps["theme"]}
      className="toaster group"
      // bg-popover: a toast floats above the cards like any overlay.
      toastOptions={{
        classNames: {
          toast:
            "group toast group-[.toaster]:bg-popover group-[.toaster]:text-popover-foreground group-[.toaster]:border-border group-[.toaster]:shadow-lg",
          description: "group-[.toast]:text-fg-tertiary",
          actionButton:
            "group-[.toast]:bg-accent-solid group-[.toast]:text-accent-solid-fg",
          cancelButton:
            "group-[.toast]:bg-muted group-[.toast]:text-fg-tertiary",
        },
      }}
      {...props}
    />
  )
}

export { Toaster }
