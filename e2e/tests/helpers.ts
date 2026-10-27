import { expect, type Page } from '@playwright/test'

export async function login(page: Page, user: { username: string; password: string }) {
  await page.goto('/login')
  await page.getByLabel('Username').fill(user.username)
  await page.getByLabel('Password').fill(user.password)
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByTestId('whoami')).toContainText(user.username)
}

export const uid = () => Math.random().toString(36).slice(2, 8)
