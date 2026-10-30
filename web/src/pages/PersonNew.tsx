import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router'
import { api } from '../api'
import { PageHeader } from '../components/Layout'
import PersonEditor from '../components/PersonEditor'

export default function PersonNewPage() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const properties = useQuery({ queryKey: ['properties'], queryFn: api.properties })
  if (!properties.data) return null
  return (
    <div className="max-w-4xl">
      <PageHeader title="Add person" />
      <PersonEditor
        properties={properties.data}
        onCancel={() => navigate('/people')}
        onSave={async (tier, changes) => {
          const values = Object.fromEntries(Object.entries(changes).filter(([, v]) => v !== null))
          const created = await api.createPerson(tier, values)
          qc.invalidateQueries({ queryKey: ['search'] })
          qc.invalidateQueries({ queryKey: ['properties'] })
          navigate(`/people/${created.id}`)
        }}
      />
    </div>
  )
}
