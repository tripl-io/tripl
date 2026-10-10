import { createContext, useContext, useEffect, useState } from "react"
import { useMediaQuery } from "@/hooks/useMediaQuery"

export type Theme = "dark" | "light" | "system"
export type Accent = "teal" | "violet" | "lime" | "indigo" | "magenta"
export type Density = "compact" | "cozy" | "comfy"
export type ChartStyle = "line" | "line-only" | "bar"

type ThemeProviderProps = {
  children: React.ReactNode
  defaultTheme?: Theme
  defaultAccent?: Accent
  defaultDensity?: Density
  defaultChartStyle?: ChartStyle
  storageKey?: string
}

type ThemeProviderState = {
  theme: Theme
  /** What is painted: `theme`, with "system" answered from the OS. */
  resolvedTheme: "dark" | "light"
  accent: Accent
  density: Density
  chartStyle: ChartStyle
  setTheme: (theme: Theme) => void
  setAccent: (accent: Accent) => void
  setDensity: (density: Density) => void
  setChartStyle: (chartStyle: ChartStyle) => void
}

const initialState: ThemeProviderState = {
  theme: "system",
  resolvedTheme: "light",
  accent: "teal",
  density: "compact",
  chartStyle: "line",
  setTheme: () => null,
  setAccent: () => null,
  setDensity: () => null,
  setChartStyle: () => null,
}

const ThemeProviderContext = createContext<ThemeProviderState>(initialState)

const ACCENTS: Accent[] = ["teal", "violet", "lime", "indigo", "magenta"]
/**
 * Accents that were retired because they read as a status: amber was
 * the warning colour and rose sat beside danger. A stored choice moves to its
 * replacement instead of silently falling back to teal.
 */
const RETIRED_ACCENTS = new Map<string, Accent>([
  ["amber", "indigo"],
  ["rose", "magenta"],
])
const DENSITIES: Density[] = ["compact", "cozy", "comfy"]
const CHART_STYLES: ChartStyle[] = ["line", "line-only", "bar"]

const DARK_QUERY = "(prefers-color-scheme: dark)"

function readLocal<T extends string>(key: string, valid: readonly T[], fallback: T): T {
  try {
    const value = localStorage.getItem(key)
    if (value && (valid as readonly string[]).includes(value)) return value as T
  } catch {
    /* ignore */
  }
  return fallback
}

function readAccent(key: string, fallback: Accent): Accent {
  try {
    const stored = localStorage.getItem(key)
    const replacement = stored ? RETIRED_ACCENTS.get(stored) : undefined
    if (replacement) {
      localStorage.setItem(key, replacement)
      return replacement
    }
  } catch {
    /* ignore */
  }
  return readLocal<Accent>(key, ACCENTS, fallback)
}

export function ThemeProvider({
  children,
  defaultTheme = "dark",
  defaultAccent = "teal",
  defaultDensity = "compact",
  defaultChartStyle = "line",
  storageKey = "tripl-ui-theme",
}: ThemeProviderProps) {
  const [theme, setThemeState] = useState<Theme>(() =>
    readLocal<Theme>(storageKey, ["dark", "light", "system"], defaultTheme),
  )
  const [accent, setAccentState] = useState<Accent>(() =>
    readAccent(`${storageKey}-accent`, defaultAccent),
  )
  const [density, setDensityState] = useState<Density>(() =>
    readLocal<Density>(`${storageKey}-density`, DENSITIES, defaultDensity),
  )
  const [chartStyle, setChartStyleState] = useState<ChartStyle>(() =>
    readLocal<ChartStyle>(`${storageKey}-chart`, CHART_STYLES, defaultChartStyle),
  )

  // "System" follows the OS for as long as it is chosen, not just at load:
  // switching the OS to dark at sunset used to leave the app light until a
  // reload, beside a Toaster that did follow. Light where there is no
  // matchMedia.
  const systemDark = useMediaQuery(DARK_QUERY)
  const resolvedTheme: "dark" | "light" =
    theme === "system" ? (systemDark ? "dark" : "light") : theme

  useEffect(() => {
    const root = window.document.documentElement
    root.classList.remove("light", "dark")
    root.classList.add(resolvedTheme)
    // The UA paints scrollbars, date pickers, autofill and number spinners from
    // `color-scheme`, which otherwise follows the OS rather than this choice:
    // Light in-app on a dark OS got dark native controls on a light page, and
    // the reverse.
    root.style.colorScheme = resolvedTheme
  }, [resolvedTheme])

  useEffect(() => {
    const root = window.document.documentElement
    ACCENTS.forEach((a) => root.classList.remove(`accent-${a}`))
    root.classList.add(`accent-${accent}`)
  }, [accent])

  useEffect(() => {
    const root = window.document.documentElement
    DENSITIES.forEach((d) => root.classList.remove(`density-${d}`))
    root.classList.add(`density-${density}`)
  }, [density])

  const value: ThemeProviderState = {
    theme,
    resolvedTheme,
    accent,
    density,
    chartStyle,
    setTheme: (next) => {
      try { localStorage.setItem(storageKey, next) } catch { /* ignore */ }
      setThemeState(next)
    },
    setAccent: (next) => {
      try { localStorage.setItem(`${storageKey}-accent`, next) } catch { /* ignore */ }
      setAccentState(next)
    },
    setDensity: (next) => {
      try { localStorage.setItem(`${storageKey}-density`, next) } catch { /* ignore */ }
      setDensityState(next)
    },
    setChartStyle: (next) => {
      try { localStorage.setItem(`${storageKey}-chart`, next) } catch { /* ignore */ }
      setChartStyleState(next)
    },
  }

  return (
    <ThemeProviderContext value={value}>
      {children}
    </ThemeProviderContext>
  )
}

// eslint-disable-next-line react-refresh/only-export-components
export const useTheme = () => {
  const context = useContext(ThemeProviderContext)
  if (context === undefined)
    throw new Error("useTheme must be used within a ThemeProvider")
  return context
}
