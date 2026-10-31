import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import type { Invocation, InvocationStatus, ToolEvent } from '../types'
import ActivityFeed from './ActivityFeed'

const KINDS = ['step', 'agent', 'provider', 'write', 'note', 'error', 'result'] as const

export type InvocationState = 'idle' | InvocationStatus

/** Run a tool on one record without blocking: start it, watch its activity, stop it before it
 * commits, and read its result once it has finished. */
export function useInvocation<T>() {
  const [handle, setHandle] = useState<Invocation | null>(null)
  const [events, setEvents] = useState<ToolEvent[]>([])
  const [result, setResult] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [status, setStatus] = useState<InvocationState>('idle')
  const source = useRef<EventSource | null>(null)

  const reset = useCallback(() => {
    source.current?.close()
    setHandle(null)
    setEvents([])
    setResult(null)
    setError(null)
    setStatus('idle')
  }, [])

  useEffect(() => () => source.current?.close(), [])

  const finish = useCallback(async (id: string) => {
    const final = await api.invocation(id)
    setHandle(final)
    setStatus(final.status)
    if (final.status === 'done') setResult((final.result ?? null) as T | null)
    else setError(final.error ?? (final.status === 'stopped' ? 'Stopped; nothing was written.' : 'Failed'))
  }, [])

  const start = useCallback(
    async (begin: () => Promise<Invocation>) => {
      reset()
      setStatus('running')
      let h: Invocation
      try {
        h = await begin()
      } catch (e) {
        setStatus('failed')
        setError(e instanceof Error ? e.message : String(e))
        return
      }
      setHandle(h)
      if (typeof EventSource === 'undefined') {
        await finish(h.id)
        return
      }
      const es = new EventSource(h.events_url)
      source.current = es
      const onMessage = (e: MessageEvent) => {
        const d = JSON.parse(e.data) as ToolEvent
        setEvents((prev) => (prev.some((x) => x.seq === d.seq) ? prev : [...prev, d]))
        if (d.kind === 'result') {
          es.close()
          void finish(h.id)
        }
      }
      for (const k of KINDS) es.addEventListener(k, onMessage)
    },
    [finish, reset],
  )

  const stop = useCallback(async () => {
    if (handle && status === 'running') await api.stopInvocation(handle.id).catch(() => undefined)
  }, [handle, status])

  return { start, stop, reset, handle, events, result, error, status }
}

/** The live view of a running invocation: its activity and a Stop button. */
export function InvocationPanel({ events, status, onStop, label }: { events: ToolEvent[]; status: InvocationState; onStop: () => void; label: string }) {
  if (status === 'idle') return null
  return (
    <div className="space-y-2 rounded-md border border-slate-200 bg-slate-50 p-3" data-testid="invocation">
      <div className="flex items-center justify-between text-xs text-slate-600">
        <span>
          {label} · <span className="font-medium">{status}</span>
        </span>
        {status === 'running' && (
          <button type="button" className="btn-ghost" onClick={onStop}>
            Stop
          </button>
        )}
      </div>
      <ActivityFeed events={events} compact />
    </div>
  )
}
