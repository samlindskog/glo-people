import { api } from '../api'
import KeyManager, { McpSnippet } from '../components/KeyManager'
import { PageHeader } from '../components/Layout'

export default function ApiKeysPage() {
  return (
    <div className="max-w-5xl space-y-6">
      <PageHeader title="My API keys" />
      <p className="text-sm text-slate-600">
        API keys let MCP clients and scripts search people as you, with your role’s permissions. Keys can’t manage
        accounts or issue other keys.
      </p>
      <KeyManager queryKey={['my-keys']} list={api.myKeys} issue={api.issueMyKey} revoke={api.revokeMyKey} />
      <div className="card p-4">
        <h2 className="text-sm font-semibold">Connecting an MCP client</h2>
        <McpSnippet />
      </div>
    </div>
  )
}
