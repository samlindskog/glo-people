import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router'
import { AuthProvider, RequireAuth } from './auth'
import Layout from './components/Layout'
import './index.css'
import AccountsPage from './pages/Accounts'
import AssistantsPage from './pages/Assistants'
import BoardEditorPage from './pages/BoardEditor'
import BoardJobsPage from './pages/BoardJobs'
import BoardTalentPage from './pages/BoardTalent'
import BoardsPage from './pages/Boards'
import HistoryPage from './pages/History'
import LoginPage from './pages/Login'
import PeoplePage from './pages/People'
import PersonPage from './pages/Person'
import PersonNewPage from './pages/PersonNew'
import PropertiesPage from './pages/Properties'

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
              <Route path="/people/:id" element={<RequireAuth permission="people:read"><PersonPage /></RequireAuth>} />
              <Route path="/properties" element={<RequireAuth permission="people:read"><PropertiesPage /></RequireAuth>} />
              <Route path="/assistants" element={<RequireAuth permission="people:read"><AssistantsPage /></RequireAuth>} />
              <Route path="/assistants/:runId" element={<RequireAuth permission="people:read"><AssistantsPage /></RequireAuth>} />
              <Route path="/boards" element={<RequireAuth permission="people:read"><BoardsPage /></RequireAuth>} />
              <Route path="/boards/new" element={<RequireAuth permission="people:write"><BoardEditorPage /></RequireAuth>} />
              <Route path="/boards/jobs" element={<RequireAuth permission="people:read"><BoardJobsPage /></RequireAuth>} />
              <Route path="/boards/talent" element={<RequireAuth permission="people:read"><BoardTalentPage /></RequireAuth>} />
              <Route path="/boards/:id" element={<RequireAuth permission="people:read"><BoardEditorPage /></RequireAuth>} />
              <Route path="/history" element={<RequireAuth permission="searches:read_own"><HistoryPage /></RequireAuth>} />
              <Route path="/accounts" element={<RequireAuth permission="accounts:manage"><AccountsPage /></RequireAuth>} />
              <Route path="*" element={<p className="text-sm text-slate-500">Page not found.</p>} />
            </Route>
          </Routes>
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
