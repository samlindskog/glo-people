import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router'
import { api } from '../api'
import { PageHeader } from '../components/Layout'
import PersonEditor from '../components/PersonEditor'

export default function PersonNewPage() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const fields = useQuery({ queryKey: ['fields'], queryFn: api.fields })
  if (!fields.data) return null
  return (
    <div className="max-w-4xl">
      <PageHeader title="Add person" />
      <PersonEditor
        fields={fields.data}
        onCancel={() => navigate('/people')}
        onSave={async (tier, changes) => {
          const values = Object.fromEntries(Object.entries(changes).filter(([, v]) => v !== null))
          const created = await api.createPerson(tier, values)
          qc.invalidateQueries({ queryKey: ['search'] })
          qc.invalidateQueries({ queryKey: ['fields'] })
          navigate(`/people/${created.id}`)
        }}
      />
    </div>
  )
}
