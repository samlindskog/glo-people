import { useEffect, useState } from 'react'
import type { AssistantRun, RunEvent, RunStatus, ToolEvent } from '../types'

const KINDS = ['step', 'agent', 'provider', 'write', 'note', 'error', 'result', 'item', 'run'] as const
const TERMINAL: RunStatus[] = ['done', 'failed', 'cancelled']

interface RunFeed {
  key: number | null
  events: RunEvent[]
  run: AssistantRun | null
  done: boolean
}

const EMPTY_RUN: RunFeed = { key: null, events: [], run: null, done: false }

/** Follow a run's Server-Sent Events: its persisted activity (replayed, then live) and its state,
 * until it has finished. The browser reconnects by itself, resuming after the last event id. */
export function useRunEvents(runId: number | null, enabled = true) {
  const [feed, setFeed] = useState<RunFeed>(EMPTY_RUN)
  const current = feed.key === runId ? feed : EMPTY_RUN

  useEffect(() => {
    if (runId === null || !enabled || typeof EventSource === 'undefined') return
    const fresh = (prev: RunFeed): RunFeed => (prev.key === runId ? prev : { ...EMPTY_RUN, key: runId })
    const es = new EventSource(`/api/assistants/runs/${runId}/events`)
    const onMessage = (e: MessageEvent) => {
      const d = JSON.parse(e.data)
      if (d.type === 'run') {
        const done = TERMINAL.includes(d.status)
        setFeed((prev) => ({ ...fresh(prev), run: d as AssistantRun, done }))
        if (done) es.close()
      } else {
        setFeed((prev) => {
          const base = fresh(prev)
          return base.events.some((x) => x.id === d.id) ? base : { ...base, events: [...base.events, d as RunEvent] }
        })
      }
    }
    for (const k of KINDS) es.addEventListener(k, onMessage)
    return () => es.close()
  }, [runId, enabled])

  return { events: current.events, run: current.run, done: current.done }
}

interface ToolFeed {
  key: string | null
  events: ToolEvent[]
  done: boolean
}

const EMPTY_TOOL: ToolFeed = { key: null, events: [], done: false }

/** Follow one tool invocation's events until its final `result` event. */
export function useInvocationEvents(eventsUrl: string | null) {
  const [feed, setFeed] = useState<ToolFeed>(EMPTY_TOOL)
  const current = feed.key === eventsUrl ? feed : EMPTY_TOOL

  useEffect(() => {
    if (!eventsUrl || typeof EventSource === 'undefined') return
    const fresh = (prev: ToolFeed): ToolFeed => (prev.key === eventsUrl ? prev : { ...EMPTY_TOOL, key: eventsUrl })
    const es = new EventSource(eventsUrl)
    const onMessage = (e: MessageEvent) => {
      const d = JSON.parse(e.data) as ToolEvent
      setFeed((prev) => {
        const base = fresh(prev)
        const events = base.events.some((x) => x.seq === d.seq) ? base.events : [...base.events, d]
        return { ...base, events, done: base.done || d.kind === 'result' }
      })
      if (d.kind === 'result') es.close()
    }
    for (const k of KINDS) es.addEventListener(k, onMessage)
    return () => es.close()
  }, [eventsUrl])

  return { events: current.events, done: current.done }
}
