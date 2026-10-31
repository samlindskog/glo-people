import { useMutation, useQuery } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router'
import { api } from '../api'
import { useRunEvents } from '../hooks/useEvents'
import { describeSearch, encodeSearch } from '../lib'
import type { AskResult, Citation, Tier } from '../types'
import ActivityFeed from './ActivityFeed'
import { ErrorNote } from './Layout'

const DEFAULTS = { k: 12, mode: 'avg' as const, threshold: 0.4 }

/** Ask a question about people: an ask run plans the search, runs it and answers with
 * citations; its steps stream in while it works. The executed search can be opened as an
 * ordinary search, and the run is on the Assistants page like any other. */
export default function Ask({ tier }: { tier: Tier | null }) {
  const navigate = useNavigate()
  const [question, setQuestion] = useState('')
  const [k, setK] = useState(DEFAULTS.k)
  const [mode, setMode] = useState<'avg' | 'max'>(DEFAULTS.mode)
  const [threshold, setThreshold] = useState(DEFAULTS.threshold)
  const [runId, setRunId] = useState<number | null>(null)
  const start = useMutation({
    mutationFn: () => api.startRun('ask', { options: { question: question.trim(), tier, k, mode, threshold } }),
    onSuccess: (run) => setRunId(run.id),
  })
  const { events, done } = useRunEvents(runId)
  const detail = useQuery({ queryKey: ['assistant-run', runId], queryFn: () => api.run(runId!), enabled: runId !== null && done })
  const item = detail.data?.items[0]
  const result = item?.status === 'done' ? (item.result as unknown as AskResult) : null
  const pending = runId !== null && !done
  const failed = item && item.status !== 'done' ? item.error ?? 'The question could not be answered.' : null

  function submit(e: FormEvent) {
    e.preventDefault()
    if (question.trim()) {
      setRunId(null)
      start.mutate()
    }
  }

  return (
    <section className="card space-y-3 p-4" aria-label="Ask">
      <form onSubmit={submit} className="flex flex-wrap items-end gap-2">
        <label className="min-w-64 flex-1 space-y-1">
          <span className="label">Ask about people</span>
          <input
            className="input"
            aria-label="Question"
            placeholder="e.g. fintech founders in the Bay Area with MRR over 10k"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
          />
        </label>
        <button className="btn-primary" disabled={start.isPending || pending || !question.trim()}>
          {start.isPending || pending ? 'Thinking…' : 'Ask'}
        </button>
      </form>
      <details className="text-sm">
        <summary className="cursor-pointer select-none text-slate-600">Advanced</summary>
        <div className="mt-2 flex flex-wrap items-end gap-4">
          <label className="space-y-1">
            <span className="label">People to read</span>
            <input type="number" className="input w-24" aria-label="People to read" min={1} max={50} value={k} onChange={(e) => setK(Number(e.target.value))} />
          </label>
          <label className="space-y-1">
            <span className="label">Combine properties by</span>
            <select className="input w-32" aria-label="Mode" value={mode} onChange={(e) => setMode(e.target.value as 'avg' | 'max')}>
              <option value="avg">Average</option>
              <option value="max">Best property</option>
            </select>
          </label>
          <label className="space-y-1">
            <span className="label">Similarity threshold</span>
            <input type="number" className="input w-24" aria-label="Ask threshold" min={-1} max={1} step={0.05} value={threshold} onChange={(e) => setThreshold(Number(e.target.value))} />
          </label>
        </div>
      </details>
      <ErrorNote error={start.error ?? detail.error ?? failed} />
      {pending && <ActivityFeed events={events} compact empty="Starting…" />}
      {result && runId !== null && (
        <AskAnswer result={result} runId={runId} onRunAsSearch={() => navigate(`/people?s=${encodeSearch({ ...result.request, page: 1 })}`)} />
      )}
    </section>
  )
}

function groupCitations(citations: Citation[]): Map<string, Citation[]> {
  const out = new Map<string, Citation[]>()
  for (const c of citations) out.set(c.person_id, [...(out.get(c.person_id) ?? []), c])
  return out
}

export function AskAnswer({ result, runId, onRunAsSearch }: { result: AskResult; runId?: number; onRunAsSearch: () => void }) {
  const names = new Map(result.results.map((h) => [h.id, h.person.name]))
  const grouped = groupCitations(result.citations)
  return (
    <div className="space-y-3 border-t border-slate-100 pt-3" data-testid="ask-result">
      <p className="whitespace-pre-wrap text-sm" data-testid="ask-answer">
        {result.text}
      </p>
      {grouped.size > 0 && (
        <ul className="space-y-1 text-xs text-slate-600" aria-label="Citations">
          {[...grouped.entries()].map(([pid, cites]) => (
            <li key={pid}>
              <Link to={`/people/${pid}`} className="font-medium text-brand-700 hover:underline">
                {names.get(pid) ?? pid}
              </Link>
              {cites.map((c, i) => (
                <span key={i} className="ml-2">
                  <span className="font-mono">{c.field}</span>: “{c.value}”
                </span>
              ))}
            </li>
          ))}
        </ul>
      )}
      <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
        <span data-testid="ask-plan">
          Searched: {describeSearch(result.request)} · {result.total.toLocaleString()} {result.total === 1 ? 'person' : 'people'}
        </span>
        {result.rationale && <span title="Planner's rationale">— {result.rationale}</span>}
        {runId !== undefined && (
          <Link to={`/assistants/${runId}`} className="text-brand-700 hover:underline">
            run #{runId}
          </Link>
        )}
        <button type="button" className="btn-secondary ml-auto" onClick={onRunAsSearch}>
          Run as search
        </button>
      </div>
    </div>
  )
}
