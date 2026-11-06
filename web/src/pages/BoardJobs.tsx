import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { Link } from 'react-router'
import { api } from '../api'
import { useAuth } from '../auth'
import { BoardsNav, Field, HtmlEditor, slugTaken, slugify } from '../components/boards'
import { ErrorNote, PageHeader } from '../components/Layout'
import type { BoardCompany, BoardJob } from '../types'

const EMPTY_JOB = { slug: '', company: '', title: '', location: '', type: '', comp: '', summary: '', html: '', status: 'open' as 'open' | 'closed', source_url: '' }
const EMPTY_CO = { slug: '', name: '', web: '', line: '', about_html: '' }

/** /boards/jobs — the shared job catalogue: companies and their roles, each with an HTML body. */
export default function BoardJobsPage() {
  const { can } = useAuth()
  const editable = can('people:write')
  const qc = useQueryClient()
  const companies = useQuery({ queryKey: ['board-companies'], queryFn: api.boardCompanies })
  const [filter, setFilter] = useState({ company: '', status: '', q: '' })
  const jobs = useQuery({ queryKey: ['board-jobs', filter], queryFn: () => api.boardJobs(filter) })
  const [job, setJob] = useState<typeof EMPTY_JOB | null>(null)
  const [jobBoards, setJobBoards] = useState<BoardJob['boards']>([])
  const [co, setCo] = useState<typeof EMPTY_CO | null>(null)
  const [jobIsNew, setJobIsNew] = useState(false)
  const [coIsNew, setCoIsNew] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [note, setNote] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['board-jobs'] })
    qc.invalidateQueries({ queryKey: ['board-companies'] })
  }

  async function editJob(j: BoardJob) {
    const full = await api.boardJob(j.slug)
    setJob({ slug: full.slug, company: full.company_slug, title: full.title, location: full.location, type: full.type, comp: full.comp, summary: full.summary, html: full.html, status: full.status, source_url: full.source_url })
    setJobBoards(full.boards ?? [])
    setJobIsNew(false)
    setNote(null)
  }

  async function saveJob(e: FormEvent) {
    e.preventDefault()
    if (!job) return
    setBusy(true)
    setError(null)
    try {
      const { slug, ...body } = job
      const key = slug.trim() || slugify(`${body.company} ${body.title}`)
      if (jobIsNew && (await slugTaken(api.boardJob, key)))
        throw new Error(`A job with the slug ${key} already exists. Edit that one, or give this one its own slug.`)
      const res = await api.putBoardJob(key, body)
      setJobIsNew(false)
      setJob({ ...job, slug: res.slug })
      setNote(res.stripped?.length ? `Saved ${res.slug}. Dropped: ${res.stripped.map((t) => `<${t}>`).join(', ')}` : `Saved ${res.slug}.`)
      refresh()
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  async function saveCo(e: FormEvent) {
    e.preventDefault()
    if (!co) return
    setBusy(true)
    setError(null)
    try {
      const { slug, ...body } = co
      const key = slug.trim() || slugify(body.name)
      if (coIsNew && cos.some((c) => c.slug === key)) throw new Error(`A company with the slug ${key} already exists.`)
      const res = await api.putBoardCompany(key, body)
      setCo(null)
      setNote(res.stripped?.length ? `Saved ${res.name}. Dropped: ${res.stripped.map((t) => `<${t}>`).join(', ')}` : `Saved ${res.name}.`)
      refresh()
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  async function remove(kind: 'job' | 'company', slug: string, label: string) {
    if (!confirm(`Delete ${label}? ${kind === 'job' ? 'It leaves every board that lists it.' : ''}`)) return
    setError(null)
    try {
      if (kind === 'job') await api.deleteBoardJob(slug)
      else await api.deleteBoardCompany(slug)
      if (kind === 'job' && job?.slug === slug) setJob(null)
      refresh()
    } catch (err) {
      setError(err)
    }
  }

  const cos = companies.data ?? []

  return (
    <div>
      <PageHeader title="Job catalogue">
        {editable && (
          <>
            <button
              className="btn-secondary"
              onClick={() => {
                setCo(co ? null : EMPTY_CO)
                setCoIsNew(true)
              }}
            >
              Add company
            </button>
            <button
              className="btn-primary"
              onClick={() => {
                setJob(job && jobIsNew ? null : { ...EMPTY_JOB, company: cos[0]?.slug ?? '' })
                setJobIsNew(true)
              }}
              disabled={cos.length === 0}
            >
              Add job
            </button>
          </>
        )}
      </PageHeader>
      <BoardsNav />
      <ErrorNote error={error ?? companies.error ?? jobs.error} />
      {note && (
        <p className="mb-3 text-sm text-emerald-700" role="status">
          {note}
        </p>
      )}

      {co && (
        <form onSubmit={saveCo} className="card mb-4 space-y-3 p-4" data-testid="company-form">
          <div className="grid gap-3 sm:grid-cols-4">
            <Field label="Name">
              <input className="input" value={co.name} onChange={(e) => setCo({ ...co, name: e.target.value })} required />
            </Field>
            <Field label="Slug" hint="Blank: from the name">
              <input className="input" value={co.slug} onChange={(e) => setCo({ ...co, slug: e.target.value })} />
            </Field>
            <Field label="Website" hint="Host only, e.g. usefastlane.ai">
              <input className="input" value={co.web} onChange={(e) => setCo({ ...co, web: e.target.value })} />
            </Field>
            <Field label="One-liner">
              <input className="input" value={co.line} onChange={(e) => setCo({ ...co, line: e.target.value })} />
            </Field>
          </div>
          <HtmlEditor label="About (shown on every role page)" value={co.about_html} onChange={(html) => setCo({ ...co, about_html: html })} rows={5} />
          <div className="flex gap-2">
            <button type="submit" className="btn-primary" disabled={busy}>
              Save company
            </button>
            <button type="button" className="btn-ghost" onClick={() => setCo(null)}>
              Cancel
            </button>
          </div>
        </form>
      )}

      {cos.length > 0 && (
        <div className="mb-4 flex flex-wrap gap-2 text-sm">
          {cos.map((c) => (
            <span key={c.slug} className="badge bg-slate-100 text-slate-700">
              {c.name}
              {editable && (
                <>
                  <button type="button" className="ml-1 text-brand-700 hover:underline" onClick={() => {
                      setCo({ slug: c.slug, name: c.name, web: c.web, line: c.line, about_html: c.about_html })
                      setCoIsNew(false)
                    }}
                  >
                    edit
                  </button>
                  <button type="button" className="ml-1 text-red-700 hover:underline" onClick={() => void remove('company', c.slug, c.name)}>
                    ✕
                  </button>
                </>
              )}
            </span>
          ))}
        </div>
      )}
      {cos.length === 0 && companies.isSuccess && <p className="mb-4 text-sm text-slate-500">Add a company first; every job belongs to one.</p>}

      {job && (
        <form onSubmit={saveJob} className="card mb-4 space-y-3 p-4" data-testid="job-form">
          <div className="grid gap-3 sm:grid-cols-3">
            <Field label="Company">
              <select className="input" value={job.company} onChange={(e) => setJob({ ...job, company: e.target.value })} required>
                {cos.map((c: BoardCompany) => (
                  <option key={c.slug} value={c.slug}>
                    {c.name}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Title">
              <input className="input" value={job.title} onChange={(e) => setJob({ ...job, title: e.target.value })} required />
            </Field>
            <Field label="Slug" hint="Blank: company + title">
              <input className="input" value={job.slug} onChange={(e) => setJob({ ...job, slug: e.target.value })} />
            </Field>
            <Field label="Location">
              <input className="input" value={job.location} onChange={(e) => setJob({ ...job, location: e.target.value })} />
            </Field>
            <Field label="Type">
              <input className="input" value={job.type} onChange={(e) => setJob({ ...job, type: e.target.value })} placeholder="Full-time" />
            </Field>
            <Field label="Pay">
              <input className="input" value={job.comp} onChange={(e) => setJob({ ...job, comp: e.target.value })} placeholder="$150K–$275K base + equity" />
            </Field>
            <Field label="Status">
              <select className="input" value={job.status} onChange={(e) => setJob({ ...job, status: e.target.value as 'open' | 'closed' })}>
                <option value="open">Open</option>
                <option value="closed">Closed (hidden from every board)</option>
              </select>
            </Field>
            <Field label="Source URL">
              <input className="input" value={job.source_url} onChange={(e) => setJob({ ...job, source_url: e.target.value })} />
            </Field>
          </div>
          <Field label="Summary" hint="One or two sentences for the list and link previews">
            <input className="input" value={job.summary} onChange={(e) => setJob({ ...job, summary: e.target.value })} />
          </Field>
          <HtmlEditor label="Description" value={job.html} onChange={(html) => setJob({ ...job, html })} />
          {jobBoards && jobBoards.length > 0 && (
            <p className="text-xs text-slate-500">
              On {jobBoards.length} board{jobBoards.length === 1 ? '' : 's'}:{' '}
              {jobBoards.map((b) => (
                <Link key={b.id} to={`/boards/${b.id}`} className="mr-2 text-brand-700 hover:underline">
                  {b.person_name}
                </Link>
              ))}
            </p>
          )}
          <div className="flex gap-2">
            <button type="submit" className="btn-primary" disabled={busy}>
              {busy ? 'Saving…' : 'Save job'}
            </button>
            <button type="button" className="btn-ghost" onClick={() => setJob(null)}>
              Close
            </button>
          </div>
        </form>
      )}

      <div className="mb-3 flex flex-wrap gap-2">
        <select className="input w-auto" value={filter.company} onChange={(e) => setFilter({ ...filter, company: e.target.value })} aria-label="Company filter">
          <option value="">All companies</option>
          {cos.map((c) => (
            <option key={c.slug} value={c.slug}>
              {c.name}
            </option>
          ))}
        </select>
        <select className="input w-auto" value={filter.status} onChange={(e) => setFilter({ ...filter, status: e.target.value })} aria-label="Status filter">
          <option value="">Open and closed</option>
          <option value="open">Open</option>
          <option value="closed">Closed</option>
        </select>
        <input className="input w-64" placeholder="Search…" value={filter.q} onChange={(e) => setFilter({ ...filter, q: e.target.value })} aria-label="Search jobs" />
      </div>
      {jobs.data && jobs.data.length === 0 && <p className="text-sm text-slate-500">No jobs.</p>}
      {jobs.data && jobs.data.length > 0 && (
        <div className="card overflow-x-auto">
          <table className="data-table" data-testid="jobs-table">
            <thead>
              <tr>
                <th>Company</th>
                <th>Title</th>
                <th>Location</th>
                <th>Pay</th>
                <th>Status</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {jobs.data.map((j) => (
                <tr key={j.slug} className={j.status === 'closed' ? 'opacity-60' : ''}>
                  <td>{j.company_name}</td>
                  <td>
                    <button type="button" className="text-left font-medium text-brand-700 hover:underline" onClick={() => void editJob(j)}>
                      {j.title}
                    </button>
                    <div className="text-xs text-slate-400">{j.slug}</div>
                  </td>
                  <td>{j.location}</td>
                  <td>{j.comp}</td>
                  <td>{j.status}</td>
                  <td className="whitespace-nowrap">
                    {editable && (
                      <button className="btn-ghost text-red-700" onClick={() => void remove('job', j.slug, j.title)}>
                        Delete
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

