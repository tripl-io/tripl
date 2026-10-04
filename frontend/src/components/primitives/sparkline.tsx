import { memo, useId, type CSSProperties } from "react"
import { SERIES_COLORS } from "@/components/ui/chart-format"
import { Skeleton } from "@/components/ui/skeleton"

export type SparklineVariant = "line" | "line-only" | "bar"

type SparklineProps = {
  data: number[]
  color?: string
  width?: number
  height?: number
  variant?: SparklineVariant
  anomalyIdx?: number | null
  className?: string
  style?: CSSProperties
  /**
   * Stretch to the container's width: `width`/`height` then only set the
   * drawing's coordinate space (viewBox) and the rendered height.
   */
  responsive?: boolean
}

/**
 * The value span the drawing scales to. Only a flat series needs a stand-in
 * (anything non-zero avoids dividing by zero). This used to be
 * `Math.max(1, max - min)`, which clamped every span below one unit to one: a
 * conversion rate stored as a fraction moving from 0.05 to 0.09 got 0.04 of
 * the height, under a pixel, and every percent or ratio metric drew a flat
 * line that read as "no change".
 */
function sparklineRange(min: number, max: number): number {
  return max - min || Math.abs(max) || 1
}

function SparklineInner({
  data,
  // The fixed single-series hue, not the user's accent: a volume line
  // read lime on Overview under one accent and teal under another, while the
  // same series drew blue in the charts.
  color = SERIES_COLORS[0],
  width = 80,
  height = 22,
  variant = "line",
  anomalyIdx = null,
  className,
  style,
  responsive = false,
}: SparklineProps) {
  const gradId = useId()
  if (!data?.length) return null
  const size = responsive
    ? {
        width: '100%',
        height,
        viewBox: `0 0 ${width} ${height}`,
        preserveAspectRatio: 'none',
      }
    : { width, height }

  const min = Math.min(...data)
  const max = Math.max(...data)

  if (variant === "bar") {
    // Bars stand on zero, not on the smallest value: measured from `min`, the
    // lowest bar was 1px however large it was. A series that dips below
    // zero keeps its minimum as the floor.
    const floor = Math.min(0, min)
    const barRange = sparklineRange(floor, max)
    const barW = Math.max(1.5, width / data.length - 1)
    return (
      <svg {...size} aria-hidden="true" className={className} style={{ display: "block", ...style }}>
        {data.map((v, i) => {
          const h = Math.max(1, ((v - floor) / barRange) * (height - 2))
          const isAnom = anomalyIdx === i
          return (
            <rect
              key={i}
              x={i * (width / data.length)}
              y={height - h}
              width={barW}
              height={h}
              fill={isAnom ? "var(--danger)" : color}
              opacity={isAnom ? 1 : 0.78}
              rx={1}
            />
          )
        })}
      </svg>
    )
  }

  const range = sparklineRange(min, max)
  const stepX = width / Math.max(1, data.length - 1)
  const points = data.map<[number, number]>((v, i) => [
    i * stepX,
    height - ((v - min) / range) * (height - 4) - 2,
  ])
  const path = points
    .map((p, i) => (i === 0 ? `M${p[0]},${p[1]}` : `L${p[0]},${p[1]}`))
    .join(" ")
  const areaPath = `${path} L${width},${height} L0,${height} Z`

  return (
    <svg
      {...size}
      aria-hidden="true"
      className={className}
      style={{ display: "block", overflow: "visible", ...style }}
    >
      <defs>
        <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.35" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      {variant !== "line-only" && <path d={areaPath} fill={`url(#${gradId})`} />}
      <path
        d={path}
        fill="none"
        stroke={color}
        strokeWidth="1.4"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      {anomalyIdx != null && points[anomalyIdx] && (
        <circle
          cx={points[anomalyIdx][0]}
          cy={points[anomalyIdx][1]}
          r={2.5}
          fill="var(--danger)"
          stroke="var(--bg)"
          strokeWidth={1}
        />
      )}
    </svg>
  )
}

export const Sparkline = memo(SparklineInner)

/**
 * A sparkline whose series has not arrived: a pulsing block of the same size,
 * so a row does not claim "—" (no data) for the seconds before it fills in.
 * Reserve "—" for a series that loaded empty.
 */
export function SparklineSkeleton({
  width = 80,
  height = 22,
  responsive = false,
  className,
}: {
  width?: number
  height?: number
  responsive?: boolean
  className?: string
}) {
  return (
    <Skeleton
      data-testid="sparkline-skeleton"
      className={className}
      style={{ width: responsive ? "100%" : width, height }}
    />
  )
}
