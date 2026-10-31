import { useEffect, useRef } from 'react'
import type { EventKind } from '../types'

const KIND_CLASS: Record<string, string> = {
  step: 'bg-slate-100 text-slate-600',
  agent: 'bg-violet-50 text-violet-700',
  provider: 'bg-amber-50 text-amber-700',
  write: 'bg-emerald-50 text-emerald-700',
  note: 'bg-sky-50 text-sky-700',
  error: 'bg-red-50 text-red-700',
  result: 'bg-slate-100 text-slate-500',
  item: 'bg-brand-50 text-brand-700',
  run: 'bg-slate-200 text-slate-700',
}

export interface FeedLine {
  kind: EventKind | string
  message: string
  ts?: string
  item_id?: number | null
}

/** The lines of a tool's or a run's activity, newest at the bottom, scrolled into view as they arrive. */
export default function ActivityFeed({ events, compact = false, empty = 'Waiting…' }: { events: FeedLine[]; compact?: boolean; empty?: string }) {
  const ref = useRef<HTMLOListElement>(null)
  useEffect(() => {
    const el = ref.current
    if (el) el.scrollTop = el.scrollHeight
  }, [events.length])
  if (events.length === 0) return <p className="text-xs text-slate-400">{empty}</p>
  return (
    <ol ref={ref} className={`space-y-0.5 overflow-y-auto font-mono text-xs ${compact ? 'max-h-32' : 'max-h-72'}`} data-testid="activity-feed">
      {events.map((e, i) => (
        <li key={i} className="flex items-baseline gap-2">
          {!compact && e.ts && <span className="shrink-0 text-slate-400">{new Date(e.ts).toLocaleTimeString()}</span>}
          <span className={`badge shrink-0 ${KIND_CLASS[e.kind] ?? 'bg-slate-100 text-slate-600'}`}>{e.kind}</span>
          <span className="break-words text-slate-700">{e.message}</span>
        </li>
      ))}
    </ol>
  )
}
