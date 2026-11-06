import { useEffect, useRef, useState } from 'react'
import { NavLink } from 'react-router'
import { ApiError, api } from '../api'
import type { BoardKind, Tier } from '../types'
import { BOARD_KINDS, TIERS } from '../types'

/** The sub-navigation shared by the Boards, Jobs and People pages. */
export function BoardsNav() {
  const cls = ({ isActive }: { isActive: boolean }) =>
    `rounded-md px-2.5 py-1 text-sm ${isActive ? 'bg-brand-50 font-medium text-brand-700' : 'text-slate-600 hover:bg-slate-100'}`
  return (
    <nav className="mb-4 flex gap-1" aria-label="Boards">
      <NavLink to="/boards" end className={cls}>
        Boards
      </NavLink>
      <NavLink to="/boards/jobs" className={cls}>
        Jobs
      </NavLink>
      <NavLink to="/boards/talent" className={cls}>
        People
      </NavLink>
    </nav>
  )
}

export function kindLabel(kind: BoardKind): string {
  return BOARD_KINDS.find((k) => k.value === kind)?.label ?? kind
}

/** The public link with Copy and Open. */
export function CopyLink({ url }: { url: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <span className="inline-flex items-center gap-2">
      <code className="rounded bg-slate-100 px-1.5 py-0.5 text-xs break-all">{url}</code>
      <button
        type="button"
        className="btn-ghost"
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(url)
            setCopied(true)
            setTimeout(() => setCopied(false), 1500)
          } catch {
            /* clipboard unavailable */
          }
        }}
      >
        {copied ? 'Copied' : 'Copy'}
      </button>
      <a className="btn-ghost" href={url} target="_blank" rel="noopener noreferrer">
        Open
      </a>
    </span>
  )
}

export function Field({ label, children, hint }: { label: string; children: React.ReactNode; hint?: string }) {
  // the hint sits outside the label so the control's accessible name is the label alone
  return (
    <div>
      <label className="block">
        <span className="label">{label}</span>
        {children}
      </label>
      {hint && <span className="mt-0.5 block text-xs text-slate-500">{hint}</span>}
    </div>
  )
}

/** The server's slug rule (boards.slug), for a blank slug field. */
export function slugify(text: string): string {
  return text
    .toLowerCase()
    .replace(/&/g, ' and ')
    .replace(/\+/g, '')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '')
    .slice(0, 120)
}

/** Whether a catalogue row with this slug exists (a 404 means no; other errors propagate). */
export async function slugTaken(get: (slug: string) => Promise<unknown>, slug: string): Promise<boolean> {
  try {
    await get(slug)
    return true
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return false
    throw e
  }
}

/** Move the item at `from` to `to` (bounds-checked); returns a new array. */
export function moveItem<T>(items: T[], from: number, to: number): T[] {
  if (to < 0 || to >= items.length || from === to) return items
  const next = items.slice()
  const [it] = next.splice(from, 1)
  next.splice(to, 0, it)
  return next
}

interface HtmlEditorProps {
  label: string
  value: string
  onChange: (html: string) => void
  rows?: number
  hint?: string
}

/** A textarea for authored HTML with a live preview of its sanitised form, styled as the public
 * page shows it. The preview is an inert iframe (no scripts) fed by POST /api/boards/preview. */
export function HtmlEditor({ label, value, onChange, rows = 12, hint }: HtmlEditorProps) {
  const [preview, setPreview] = useState<{ html: string; stripped: string[] } | null>(null)
  const [showing, setShowing] = useState(false)
  const timer = useRef<number | undefined>(undefined)

  useEffect(() => {
    if (!showing) return
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => {
      api
        .boardPreview(value)
        .then(setPreview)
        .catch(() => setPreview(null))
    }, 400)
    return () => window.clearTimeout(timer.current)
  }, [value, showing])

  const doc = preview
    ? `<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="/glojobs/static/styles.css"></head><body><main class="wrap"><article class="prose">${preview.html}</article></main></body></html>`
    : ''

  return (
    <div>
      <div className="flex items-center justify-between">
        <span className="label">{label}</span>
        <button type="button" className="btn-ghost" onClick={() => setShowing((v) => !v)} aria-pressed={showing}>
          {showing ? 'Hide preview' : 'Preview'}
        </button>
      </div>
      <div className={showing ? 'grid gap-3 md:grid-cols-2' : ''}>
        <textarea className="input font-mono text-xs" rows={rows} value={value} onChange={(e) => onChange(e.target.value)} aria-label={label} spellCheck={false} />
        {showing && (
          <div>
            <iframe title={`${label} preview`} sandbox="" srcDoc={doc} className="h-72 w-full rounded-md border border-slate-200 bg-white" />
            {preview && preview.stripped.length > 0 && (
              <p className="mt-1 text-xs text-amber-700" data-testid="stripped">
                Dropped on the page: {preview.stripped.map((t) => `<${t}>`).join(', ')}
              </p>
            )}
          </div>
        )}
      </div>
      <span className="mt-0.5 block text-xs text-slate-500">
        {hint ?? 'HTML: h2 headings, p, ul/li, strong, em, a. Scripts, styles and images are dropped.'}
      </span>
    </div>
  )
}

export interface PickedPerson {
  id: string
  name: string
}

interface PersonPickerProps {
  value: PickedPerson | null
  onChange: (p: PickedPerson | null) => void
  /** Whether the inline "create person" form is offered (needs people:write). */
  canCreate?: boolean
}

/** Finds a person by name; when nobody matches, a small form creates the record on the spot. */
export function PersonPicker({ value, onChange, canCreate = true }: PersonPickerProps) {
  const [q, setQ] = useState('')
  const [hits, setHits] = useState<PickedPerson[]>([])
  const [searched, setSearched] = useState(false)
  const [creating, setCreating] = useState(false)
  const [form, setForm] = useState({ name: '', email: '', company: '', role: '', tier: 'plus_two' as Tier })
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const timer = useRef<number | undefined>(undefined)

  const term = q.trim()
  const active = term.length >= 2
  useEffect(() => {
    if (!active) return
    timer.current = window.setTimeout(() => {
      api
        .search({ filters: [{ field: 'name', op: 'contains', value: term }], sort: { field: 'name', dir: 'asc' }, limit: 8, page: 1 })
        .then((res) => {
          setHits(res.results.map((h) => ({ id: h.id, name: h.person.name })))
          setSearched(true)
        })
        .catch(() => setHits([]))
    }, 300)
    return () => window.clearTimeout(timer.current)
  }, [term, active])
  const shown = active ? hits : []
  const nobody = active && searched && hits.length === 0

  if (value) {
    return (
      <div className="flex items-center gap-2 text-sm" data-testid="picked-person">
        <span className="font-medium">{value.name}</span>
        <a className="text-xs text-brand-600 hover:underline" href={`/people/${value.id}`} target="_blank" rel="noopener noreferrer">
          record
        </a>
        <button type="button" className="btn-ghost" onClick={() => onChange(null)}>
          Change
        </button>
      </div>
    )
  }

  async function create() {
    if (!form.name.trim()) {
      setError('A name is needed.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const fields: Record<string, unknown> = { name: form.name.trim() }
      for (const k of ['email', 'company', 'role'] as const) if (form[k].trim()) fields[k] = form[k].trim()
      const created = await api.createPerson(form.tier, fields)
      onChange({ id: created.id, name: created.name })
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-2">
      <input className="input" placeholder="Search people by name…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Person" autoComplete="off" />
      {shown.length > 0 && (
        <ul className="card divide-y divide-slate-100" data-testid="person-hits">
          {shown.map((h) => (
            <li key={h.id}>
              <button type="button" className="w-full px-3 py-1.5 text-left text-sm hover:bg-slate-50" onClick={() => onChange(h)}>
                {h.name}
              </button>
            </li>
          ))}
        </ul>
      )}
      {nobody && <p className="text-sm text-slate-500">Nobody by that name.</p>}
      {canCreate && !creating && (
        <button
          type="button"
          className="btn-secondary"
          onClick={() => {
            setForm((f) => ({ ...f, name: q.trim() }))
            setCreating(true)
          }}
        >
          Create a person
        </button>
      )}
      {creating && (
        <div className="card space-y-2 p-3" data-testid="create-person">
          <div className="grid gap-2 sm:grid-cols-2">
            <Field label="Name">
              <input className="input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            </Field>
            <Field label="Email">
              <input className="input" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
            </Field>
            <Field label="Company">
              <input className="input" value={form.company} onChange={(e) => setForm({ ...form, company: e.target.value })} />
            </Field>
            <Field label="Role">
              <input className="input" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })} />
            </Field>
            <Field label="Tier">
              <select className="input" value={form.tier} onChange={(e) => setForm({ ...form, tier: e.target.value as Tier })}>
                {TIERS.map((t) => (
                  <option key={t.value} value={t.value}>
                    {t.label}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          {error && (
            <p role="alert" className="text-sm text-red-700">
              {error}
            </p>
          )}
          <div className="flex gap-2">
            <button type="button" className="btn-primary" disabled={busy} onClick={() => void create()}>
              {busy ? 'Creating…' : 'Create and use'}
            </button>
            <button type="button" className="btn-ghost" onClick={() => setCreating(false)}>
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

export interface SlugItem {
  slug: string
  title: string
  sub?: string
}

interface SlugListProps {
  label: string
  available: SlugItem[]
  selected: string[]
  onChange: (slugs: string[]) => void
  emptyText: string
}

/** An ordered pick list: the chosen items with up / down / remove, and a searchable list of
 * what can be added. */
export function SlugList({ label, available, selected, onChange, emptyText }: SlugListProps) {
  const [q, setQ] = useState('')
  const byslug = Object.fromEntries(available.map((a) => [a.slug, a]))
  const term = q.trim().toLowerCase()
  const candidates = available.filter((a) => !selected.includes(a.slug) && (!term || `${a.title} ${a.sub ?? ''}`.toLowerCase().includes(term)))
  return (
    <div className="grid gap-3 md:grid-cols-2">
      <div>
        <span className="label">
          {label} ({selected.length})
        </span>
        {selected.length === 0 ? (
          <p className="text-sm text-slate-500">{emptyText}</p>
        ) : (
          <ol className="card divide-y divide-slate-100" data-testid={`selected-${label.toLowerCase()}`}>
            {selected.map((s, i) => (
              <li key={s} className="flex items-center gap-2 px-3 py-1.5 text-sm">
                <span className="w-5 text-xs text-slate-400">{i + 1}</span>
                <span className="flex-1">
                  <span className="font-medium">{byslug[s]?.title ?? s}</span>
                  {byslug[s]?.sub && <span className="ml-1 text-slate-500">{byslug[s].sub}</span>}
                </span>
                <button type="button" className="btn-ghost" aria-label={`Move ${byslug[s]?.title ?? s} up`} disabled={i === 0} onClick={() => onChange(moveItem(selected, i, i - 1))}>
                  ↑
                </button>
                <button type="button" className="btn-ghost" aria-label={`Move ${byslug[s]?.title ?? s} down`} disabled={i === selected.length - 1} onClick={() => onChange(moveItem(selected, i, i + 1))}>
                  ↓
                </button>
                <button type="button" className="btn-ghost" aria-label={`Remove ${byslug[s]?.title ?? s}`} onClick={() => onChange(selected.filter((x) => x !== s))}>
                  ✕
                </button>
              </li>
            ))}
          </ol>
        )}
      </div>
      <div>
        <span className="label">Add</span>
        <input className="input mb-1" placeholder="Filter…" value={q} onChange={(e) => setQ(e.target.value)} aria-label={`Filter ${label.toLowerCase()}`} />
        <ul className="card max-h-64 divide-y divide-slate-100 overflow-auto">
          {candidates.length === 0 && <li className="px-3 py-1.5 text-sm text-slate-500">Nothing to add.</li>}
          {candidates.map((a) => (
            <li key={a.slug}>
              <button type="button" className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm hover:bg-slate-50" onClick={() => onChange([...selected, a.slug])}>
                <span className="flex-1">
                  <span className="font-medium">{a.title}</span>
                  {a.sub && <span className="ml-1 text-slate-500">{a.sub}</span>}
                </span>
                <span className="text-xs text-brand-600">Add</span>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
