import { useQuery } from '@tanstack/react-query'
import { api } from '../api'

/** The Firecrawl balance in the top bar: remaining credits and what running assistants have reserved. */
export default function CreditsChip() {
  const q = useQuery({ queryKey: ['credits'], queryFn: api.credits, refetchInterval: 60_000, staleTime: 30_000 })
  const c = q.data
  if (!c || c.remaining === null) return null
  const period = c.period_end ? ` · resets ${new Date(c.period_end).toLocaleDateString()}` : ''
  return (
    <span
      className={`badge ${c.remaining < 200 ? 'bg-red-50 text-red-700' : 'bg-amber-50 text-amber-700'}`}
      title={`Firecrawl credits remaining${c.plan ? ` of ${c.plan.toLocaleString()} in the plan` : ''}${period}${c.reserved ? ` · ${c.reserved} reserved by running assistants` : ''}${c.error ? ` · ${c.error}` : ''}`}
      data-testid="credits"
    >
      {c.remaining.toLocaleString()} credits
      {c.reserved > 0 && <span className="ml-1 opacity-70">· {c.reserved} reserved</span>}
    </span>
  )
}
