import { expect, type Page } from '@playwright/test'

export async function login(page: Page, user: { username: string; password: string }) {
  // Through the API so it also works on a dev stack with DEV_AUTO_LOGIN=1, where the login
  // page is skipped; the session cookie lands in the page's browser context.
  const r = await page.request.post('/api/auth/login', { data: user })
  expect(r.ok(), `login as ${user.username}: ${r.status()}`).toBeTruthy()
  await page.goto('/people')
  await expect(page.getByTestId('whoami')).toContainText(user.username)
}

export const uid = () => Math.random().toString(36).slice(2, 8)
