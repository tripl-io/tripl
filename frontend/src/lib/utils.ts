import { type ClassValue, clsx } from "clsx"
import { extendTailwindMerge } from "tailwind-merge"

// The app's own type and radius scale (index.css @theme). Without this
// tailwind-merge reads `text-body-sm` as a text COLOUR and drops it when a
// colour class follows (`cn("text-body-sm", "text-fg-subtle")`), and it would
// never let a later size override it.
// `micro-label` (index.css) sets size, weight, case and tracking at once, so a
// call site's `micro-label` replaces the base's size and weight classes
// (`cn("text-body-sm font-semibold", "micro-label")`). The reverse is not
// merge-safe (both classes stay and CSS order decides), so a kit base that
// callers resize, like TableHead, spells the four utilities out instead.
const twMerge = extendTailwindMerge<"micro-label">({
  extend: {
    theme: {
      text: ["2xs", "micro", "caption", "body-sm", "body", "lead", "heading", "title", "display"],
      radius: ["control", "card"],
    },
    classGroups: {
      "micro-label": ["micro-label"],
    },
    conflictingClassGroups: {
      "micro-label": ["font-size", "font-weight", "tracking", "leading"],
    },
  },
})

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function getErrorMessage(error: unknown, fallback = 'Something went wrong.') {
  return error instanceof Error && error.message ? error.message : fallback
}
