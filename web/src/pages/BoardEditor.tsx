import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router'
import { api } from '../api'
import { useAuth } from '../auth'
import { BoardsNav, CopyLink, Field, HtmlEditor, PersonPicker, SlugList, type PickedPerson } from '../components/boards'
import { ErrorNote, PageHeader } from '../components/Layout'
import type { BoardDetail, BoardKind } from '../types'
import { BOARD_KINDS } from '../types'

export interface Draft {
  person: PickedPerson | null
  kind: BoardKind
  title: string
  intro_html: string
  job_slugs: string[]
  talent_slugs: string[]
  enabled: boolean
}

const EMPTY: Draft = { person: null, kind: 'jobs', title: '', intro_html: '', job_slugs: [], talent_slugs: [], enabled: true }

/** The request body for a draft (the person must be set). */
export function toBody(d: Draft) {
  return {
    person_id: d.person?.id ?? '',
    kind: d.kind,
    title: d.title.trim() || null,
    intro_html: d.intro_html,
    job_slugs: d.job_slugs,
    talent_slugs: d.talent_slugs,
    enabled: d.enabled,
  }
}

/** /boards/new and /boards/:id — assign a glojobs.app page to a person and pick what it shows. */
export default function BoardEditorPage() {
  const { id } = useParams()
  const [params] = useSearchParams()
  const board = useQuery({ queryKey: ['board', id], queryFn: () => api.board(id!), enabled: !!id })
  const presetPerson = params.get('person')
  const preset = useQuery({ queryKey: ['person', presetPerson], queryFn: () => api.person(presetPerson!), enabled: !id && !!presetPerson })

  if (id && board.isError) return <ErrorNote error={board.error} />
  if (id && !board.data) return null
  if (!id && presetPerson && !preset.data && !preset.isError) return null

  const initial: Draft = board.data
    ? {
        person: { id: board.data.person_id, name: board.data.person_name },
        kind: board.data.kind,
        title: board.data.title ?? '',
        intro_html: board.data.intro_html,
        job_slugs: board.data.job_slugs,
        talent_slugs: board.data.talent_slugs,
        enabled: board.data.enabled,
      }
    : { ...EMPTY, person: preset.data ? { id: preset.data.id, name: preset.data.name } : null }
  // keyed by the board so navigating to another one starts the form over from the server
  return <Editor key={id ?? 'new'} id={id} board={board.data ?? null} initial={initial} />
}

function Editor({ id, board, initial }: { id?: string; board: BoardDetail | null; initial: Draft }) {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const { can } = useAuth()
  const editable = can('people:write')
  const jobs = useQuery({ queryKey: ['board-jobs', 'open'], queryFn: () => api.boardJobs({ status: 'open' }) })
  const talent = useQuery({ queryKey: ['board-talent', 'active'], queryFn: () => api.boardTalent({ status: 'active' }) })

  const [draft, setDraft] = useState<Draft>(initial)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState<string | null>(null)

  const set = (patch: Partial<Draft>) => setDraft((d) => ({ ...d, ...patch }))

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (!draft.person) {
      setError(new Error('Pick or create the person the page is for.'))
      return
    }
    setBusy(true)
    setError(null)
    try {
      const body = toBody(draft)
      // only the fields that changed here: a list the admin did not touch must not overwrite
      // what an agent added to it through MCP since the page loaded
      const before = toBody(initial)
      const changed = Object.fromEntries(Object.entries(body).filter(([k, v]) => JSON.stringify(v) !== JSON.stringify(before[k as keyof typeof before])))
      const res = id ? await api.updateBoard(id, changed) : await api.createBoard(body)
      qc.invalidateQueries({ queryKey: ['boards'] })
      qc.invalidateQueries({ queryKey: ['board', res.id] })
      if (res.stripped?.length) setSaved(`Saved. Dropped from the intro: ${res.stripped.map((t) => `<${t}>`).join(', ')}`)
      else setSaved('Saved.')
      if (!id) navigate(`/boards/${res.id}`, { replace: true })
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  const showJobs = draft.kind !== 'talent'
  const showPeople = draft.kind !== 'jobs'

  return (
    <div className="max-w-5xl">
      <PageHeader title={id ? `Board for ${board?.person_name ?? ''}` : 'New board'}>
        {board && <CopyLink url={board.url} />}
      </PageHeader>
      <BoardsNav />
      <ErrorNote error={error} />
      {saved && (
        <p className="mb-3 text-sm text-emerald-700" role="status">
          {saved}
        </p>
      )}
      {board && (
        <p className="mb-3 text-xs text-slate-500">
          Opened {board.views} time{board.views === 1 ? '' : 's'}
          {board.last_viewed_at && `, last ${new Date(board.last_viewed_at).toLocaleString()}`}. {!board.enabled && 'The link is disabled.'}
        </p>
      )}
      <form onSubmit={submit} className="space-y-5">
        <div className="card space-y-4 p-4">
          <Field label="Person">
            <PersonPicker value={draft.person} onChange={(p) => set({ person: p })} canCreate={editable} />
          </Field>
          <div className="grid gap-3 sm:grid-cols-3">
            <Field label="Kind">
              <select className="input" value={draft.kind} onChange={(e) => set({ kind: e.target.value as BoardKind })} disabled={!editable}>
                {BOARD_KINDS.map((k) => (
                  <option key={k.value} value={k.value}>
                    {k.label}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Title" hint="Blank: “Roles picked for <first name>”">
              <input className="input" value={draft.title} onChange={(e) => set({ title: e.target.value })} disabled={!editable} />
            </Field>
            <Field label="Link">
              <select className="input" value={draft.enabled ? 'on' : 'off'} onChange={(e) => set({ enabled: e.target.value === 'on' })} disabled={!editable}>
                <option value="on">Enabled</option>
                <option value="off">Disabled (404)</option>
              </select>
            </Field>
          </div>
          <HtmlEditor label="Intro" value={draft.intro_html} onChange={(html) => set({ intro_html: html })} rows={4} hint="A short note at the top of the page, as HTML (p, strong, a). Optional." />
        </div>

        {showJobs && (
          <div className="card p-4">
            <SlugList
              label="Jobs"
              available={(jobs.data ?? []).map((j) => ({ slug: j.slug, title: j.title, sub: `· ${j.company_name ?? j.company_slug}${j.location ? ` · ${j.location}` : ''}` }))}
              selected={draft.job_slugs}
              onChange={(s) => set({ job_slugs: s })}
              emptyText="No jobs on this page yet. Add from the open jobs on the right."
            />
            <p className="mt-2 text-xs text-slate-500">
              Only open jobs are listed; <Link to="/boards/jobs" className="text-brand-700 hover:underline">edit the catalogue</Link> to add one.
            </p>
          </div>
        )}
        {showPeople && (
          <div className="card p-4">
            <SlugList
              label="People"
              available={(talent.data ?? []).map((t) => ({ slug: t.slug, title: t.name, sub: t.headline ? `· ${t.headline}` : '' }))}
              selected={draft.talent_slugs}
              onChange={(s) => set({ talent_slugs: s })}
              emptyText="No people on this page yet. Add from the active entries on the right."
            />
            <p className="mt-2 text-xs text-slate-500">
              Only active entries are listed; <Link to="/boards/talent" className="text-brand-700 hover:underline">edit the catalogue</Link> to add one.
            </p>
          </div>
        )}

        {editable && (
          <div className="flex items-center gap-2">
            <button type="submit" className="btn-primary" disabled={busy}>
              {busy ? 'Saving…' : id ? 'Save' : 'Create board'}
            </button>
            <Link to="/boards" className="btn-ghost">
              Back to boards
            </Link>
          </div>
        )}
      </form>
    </div>
  )
}
