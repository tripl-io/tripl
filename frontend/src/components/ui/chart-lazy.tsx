import { Suspense, type ComponentProps } from 'react'
import { lazyWithReload } from '@/lib/lazyWithReload'
import type * as ChartLib from './chart'
import { ChartSkeleton } from '@/components/states'

// Lazy wrapper that keeps recharts out of pages' initial chunks. Both lazies
// resolve via the same dynamic import — Vite/Rollup deduplicates the request,
// so the chart module is fetched once even if MetricsChart and MiniMetricsChart
// mount in the same render.
const loadChartModule = () => import('./chart')

const MetricsChartImpl = lazyWithReload(() =>
  loadChartModule().then(module_ => ({ default: module_.MetricsChart })),
)
const MiniMetricsChartImpl = lazyWithReload(() =>
  loadChartModule().then(module_ => ({ default: module_.MiniMetricsChart })),
)
const MetricsMultiSeriesChartImpl = lazyWithReload(() =>
  loadChartModule().then(module_ => ({ default: module_.MetricsMultiSeriesChart })),
)

type MetricsChartProps = ComponentProps<typeof ChartLib.MetricsChart>
type MiniMetricsChartProps = ComponentProps<typeof ChartLib.MiniMetricsChart>
type MetricsMultiSeriesChartProps = ComponentProps<typeof ChartLib.MetricsMultiSeriesChart>

// A chart-shaped skeleton at the chart's own height, not a "Loading…" word in
// a blank box.
function ChartFallback({
  className,
  height,
}: {
  className?: string
  height?: number
}) {
  return <ChartSkeleton className={className} height={height} />
}

export function MetricsChart(props: MetricsChartProps) {
  return (
    <Suspense fallback={<ChartFallback className={props.className} height={props.height ?? 300} />}>
      <MetricsChartImpl {...props} />
    </Suspense>
  )
}

export function MiniMetricsChart(props: MiniMetricsChartProps) {
  return (
    <Suspense fallback={<ChartFallback className={props.className} height={props.height ?? 72} />}>
      <MiniMetricsChartImpl {...props} />
    </Suspense>
  )
}

export function MetricsMultiSeriesChart(props: MetricsMultiSeriesChartProps) {
  return (
    <Suspense fallback={<ChartFallback className={props.className} height={props.height ?? 300} />}>
      <MetricsMultiSeriesChartImpl {...props} />
    </Suspense>
  )
}
