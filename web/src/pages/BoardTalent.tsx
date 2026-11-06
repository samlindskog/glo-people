import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { Link } from 'react-router'
import { api } from '../api'
import { useAuth } from '../auth'
import { BoardsNav, Field, HtmlEditor, PersonPicker, slugTaken, slugify, type PickedPerson } from '../components/boards'
import { ErrorNote, PageHeader } from '../components/Layout'
import type { BoardTalent } from '../types'

interface Draft {
  slug: string
  person: PickedPerson | null
  name: string
  headline: string
  location: string
  summary: string
  html: string
  status: 'active' | 'hidden'
}

const EMPTY: Draft = { slug: '', person: null, name: '', headline: '', location: '', summary: '', html: '', status: 'active' }

/** /boards/talent — the shared talent catalogue: the people a talent board can show, each with
 * an HTML profile drafted from their glo-people record. Every entry is one person's: the record
 * is picked (or created) first, and the entry is what glojobs.app shows of them. */
export default function BoardTalentPage() {
  const { can } = useAuth()
  const editable = can('people:write')
  const qc = useQueryClient()
  const [filter, setFilter] = useState({ status: '', q: '' })
  const talent = useQuery({ queryKey: ['board-talent', filter], queryFn: () => api.boardTalent(filter) })
  const [draft, setDraft] = useState<Draft | null>(null)
  const [isNew, setIsNew] = useState(false)
  const [onBoards, setOnBoards] = useState<BoardTalent['boards']>([])
  const [error, setError] = useState<unknown>(null)
  const [note, setNote] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const refresh = () => qc.invalidateQueries({ queryKey: ['board-talent'] })

  async function edit(t: BoardTalent) {
    const full = await api.boardTalentOne(t.slug)
    setDraft({
      slug: full.slug,
      person: full.person_id ? { id: full.person_id, name: full.name } : null,
      name: full.name,
      headline: full.headline,
      location: full.location,
      summary: full.summary,
      html: full.html,
      status: full.status,
    })
    setOnBoards(full.boards ?? [])
    setIsNew(false)
    setNote(null)
  }

  async function fromPerson(p: PickedPerson | null) {
    if (!draft) return
    if (!p) {
      setDraft({ ...draft, person: null })
      return
    }
    setError(null)
    try {
      const d = await api.boardTalentDraft(p.id)
      setDraft({
        ...draft,
        person: p,
        slug: draft.slug || d.slug,
        name: draft.name || d.name,
        headline: draft.headline || d.headline,
        location: draft.location || d.location,
        summary: draft.summary || d.summary,
        html: draft.html || d.html,
      })
    } catch (err) {
      setError(err)
    }
  }

  async function save(e: FormEvent) {
    e.preventDefault()
    if (!draft) return
    setBusy(true)
    setError(null)
    try {
      const { slug, person, ...rest } = draft
      if (!person) throw new Error('Pick the glo-people record this entry is for first (or create one). Every entry belongs to a person.')
      const key = slug.trim() || slugify(rest.name)
      if (isNew && (await slugTaken(api.boardTalentOne, key)))
        throw new Error(`An entry with the slug ${key} already exists. Edit that one, or give this one its own slug.`)
      const res = await api.putBoardTalent(key, { ...rest, person_id: person.id })
      setIsNew(false)
      setDraft({ ...draft, slug: res.slug })
      setNote(res.stripped?.length ? `Saved ${res.name}. Dropped: ${res.stripped.map((t) => `<${t}>`).join(', ')}` : `Saved ${res.name}.`)
      refresh()
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  async function remove(t: BoardTalent) {
    if (!confirm(`Delete ${t.name} from the catalogue? They leave every board that lists them.`)) return
    setError(null)
    try {
      await api.deleteBoardTalent(t.slug)
      if (draft?.slug === t.slug) setDraft(null)
      refresh()
    } catch (err) {
      setError(err)
    }
  }

  return (
    <div>
      <PageHeader title="Talent catalogue">
        {editable && (
          <button
            className="btn-primary"
            onClick={() => {
              setDraft(draft && isNew ? null : EMPTY)
              setIsNew(true)
            }}
          >
            Add person
          </button>
        )}
      </PageHeader>
      <BoardsNav />
      <ErrorNote error={error ?? talent.error} />
      {note && (
        <p className="mb-3 text-sm text-emerald-700" role="status">
          {note}
        </p>
      )}

      {draft && (
        <form onSubmit={save} className="card mb-4 space-y-3 p-4" data-testid="talent-form">
          <Field label="glo-people record" hint="Required: the person this entry is. Picking them fills the blank fields from their record; only what you save here is shown publicly.">
            <PersonPicker value={draft.person} onChange={(p) => void fromPerson(p)} canCreate={editable} />
          </Field>
          <div className="grid gap-3 sm:grid-cols-3">
            <Field label="Name">
              <input className="input" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} required />
            </Field>
            <Field label="Slug" hint="Blank: from the name">
              <input className="input" value={draft.slug} onChange={(e) => setDraft({ ...draft, slug: e.target.value })} />
            </Field>
            <Field label="Status">
              <select className="input" value={draft.status} onChange={(e) => setDraft({ ...draft, status: e.target.value as 'active' | 'hidden' })}>
                <option value="active">Active</option>
                <option value="hidden">Hidden (off every board)</option>
              </select>
            </Field>
            <Field label="Headline" hint="Role at company">
              <input className="input" value={draft.headline} onChange={(e) => setDraft({ ...draft, headline: e.target.value })} />
            </Field>
            <Field label="Location">
              <input className="input" value={draft.location} onChange={(e) => setDraft({ ...draft, location: e.target.value })} />
            </Field>
          </div>
          <Field label="Summary" hint="One or two sentences for the card">
            <input className="input" value={draft.summary} onChange={(e) => setDraft({ ...draft, summary: e.target.value })} />
          </Field>
          <HtmlEditor label="Profile" value={draft.html} onChange={(html) => setDraft({ ...draft, html })} />
          {onBoards && onBoards.length > 0 && (
            <p className="text-xs text-slate-500">
              On {onBoards.length} board{onBoards.length === 1 ? '' : 's'}:{' '}
              {onBoards.map((b) => (
                <Link key={b.id} to={`/boards/${b.id}`} className="mr-2 text-brand-700 hover:underline">
                  {b.person_name}
                </Link>
              ))}
            </p>
          )}
          <div className="flex gap-2">
            <button type="submit" className="btn-primary" disabled={busy}>
              {busy ? 'Saving…' : 'Save'}
            </button>
            <button type="button" className="btn-ghost" onClick={() => setDraft(null)}>
              Close
            </button>
          </div>
        </form>
      )}

      <div className="mb-3 flex flex-wrap gap-2">
        <select className="input w-auto" value={filter.status} onChange={(e) => setFilter({ ...filter, status: e.target.value })} aria-label="Status filter">
          <option value="">Active and hidden</option>
          <option value="active">Active</option>
          <option value="hidden">Hidden</option>
        </select>
        <input className="input w-64" placeholder="Search…" value={filter.q} onChange={(e) => setFilter({ ...filter, q: e.target.value })} aria-label="Search talent" />
      </div>
      {talent.data && talent.data.length === 0 && <p className="text-sm text-slate-500">No one in the catalogue yet.</p>}
      {talent.data && talent.data.length > 0 && (
        <div className="card overflow-x-auto">
          <table className="data-table" data-testid="talent-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Headline</th>
                <th>Location</th>
                <th>Record</th>
                <th>Status</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {talent.data.map((t) => (
                <tr key={t.slug} className={t.status === 'hidden' ? 'opacity-60' : ''}>
                  <td>
                    <button type="button" className="text-left font-medium text-brand-700 hover:underline" onClick={() => void edit(t)}>
                      {t.name}
                    </button>
                    <div className="text-xs text-slate-400">{t.slug}</div>
                  </td>
                  <td>{t.headline}</td>
                  <td>{t.location}</td>
                  <td>
                    {t.person_id && (
                      <Link to={`/people/${t.person_id}`} className="text-brand-700 hover:underline">
                        open
                      </Link>
                    )}
                  </td>
                  <td>{t.status}</td>
                  <td className="whitespace-nowrap">
                    {editable && (
                      <button className="btn-ghost text-red-700" onClick={() => void remove(t)}>
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
