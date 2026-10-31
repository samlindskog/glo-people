import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router'
import { AuthProvider, RequireAuth } from './auth'
import Layout from './components/Layout'
import './index.css'
import AccountsPage from './pages/Accounts'
import ActivityPage from './pages/Activity'
import ApiKeysPage from './pages/ApiKeys'
import AssistantsPage from './pages/Assistants'
import ImportPage from './pages/Import'
import LoginPage from './pages/Login'
import PeoplePage from './pages/People'
import PersonPage from './pages/Person'
import PersonNewPage from './pages/PersonNew'
import PropertiesPage from './pages/Properties'
import SearchesPage from './pages/Searches'

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthProvider>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route
              element={
                <RequireAuth>
                  <Layout />
                </RequireAuth>
              }
            >
              <Route index element={<Navigate to="/people" replace />} />
              <Route path="/people" element={<RequireAuth permission="people:read"><PeoplePage /></RequireAuth>} />
              <Route path="/people/new" element={<RequireAuth permission="people:write"><PersonNewPage /></RequireAuth>} />
              <Route path="/people/import" element={<RequireAuth permission="people:write"><ImportPage /></RequireAuth>} />
              <Route path="/people/:id" element={<RequireAuth permission="people:read"><PersonPage /></RequireAuth>} />
              <Route path="/properties" element={<RequireAuth permission="people:read"><PropertiesPage /></RequireAuth>} />
              <Route path="/assistants" element={<RequireAuth permission="people:read"><AssistantsPage /></RequireAuth>} />
              <Route path="/searches" element={<RequireAuth permission="searches:read_own"><SearchesPage /></RequireAuth>} />
              <Route path="/activity" element={<RequireAuth permission="activity:read"><ActivityPage /></RequireAuth>} />
              <Route path="/accounts" element={<RequireAuth permission="accounts:manage"><AccountsPage /></RequireAuth>} />
              <Route path="/api-keys" element={<RequireAuth permission="api_keys:manage_own"><ApiKeysPage /></RequireAuth>} />
              <Route path="*" element={<p className="text-sm text-slate-500">Page not found.</p>} />
            </Route>
          </Routes>
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
