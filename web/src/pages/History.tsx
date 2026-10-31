import { useSearchParams } from 'react-router'
import { useAuth } from '../auth'
import { PageHeader } from '../components/Layout'
import ActivityPanel from './Activity'
import SearchesPanel from './Searches'

type Tab = 'searches' | 'activity'

/** What happened: the searches (yours, or everyone's) and the activity log, as two tabs. */
export default function HistoryPage() {
  const { can } = useAuth()
  const [params, setParams] = useSearchParams()
  const tabs: { key: Tab; label: string }[] = [{ key: 'searches', label: 'Searches' }]
  if (can('activity:read')) tabs.push({ key: 'activity', label: 'Activity' })
  const current = (params.get('tab') as Tab) ?? 'searches'
  const tab = tabs.some((t) => t.key === current) ? current : 'searches'

  return (
    <div>
      <PageHeader title="History">
        <div className="flex gap-1 rounded-md bg-slate-100 p-1" role="tablist" aria-label="History">
          {tabs.map((t) => (
            <button
              key={t.key}
              role="tab"
              aria-selected={tab === t.key}
              className={`rounded px-3 py-1 text-sm ${tab === t.key ? 'bg-white font-medium text-slate-900 shadow-xs' : 'text-slate-600 hover:text-slate-900'}`}
              onClick={() => setParams(t.key === 'searches' ? {} : { tab: t.key })}
            >
              {t.label}
            </button>
          ))}
        </div>
      </PageHeader>
      {tab === 'searches' ? <SearchesPanel /> : <ActivityPanel />}
    </div>
  )
}
