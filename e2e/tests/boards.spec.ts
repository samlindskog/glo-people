import { expect, test } from '@playwright/test'
import { WRITER } from '../users'
import { login, uid } from './helpers'

test('writer builds the catalogue, assigns a board to a new person and the public page shows it', async ({ page, context }) => {
  const tag = uid()
  await login(page, WRITER)

  // the catalogue: a company and a job with an HTML description
  await page.getByRole('link', { name: 'Boards' }).click()
  await page.getByRole('link', { name: 'Jobs' }).click()
  await page.getByRole('button', { name: 'Add company' }).click()
  const coForm = page.getByTestId('company-form')
  await coForm.getByLabel('Name', { exact: true }).fill(`Fastlane ${tag}`)
  await coForm.getByLabel('Website').fill('usefastlane.ai')
  await coForm.getByLabel('One-liner').fill('AI ads')
  await coForm.getByLabel('About (shown on every role page)').fill('<p>Fastlane makes ads.</p>')
  await coForm.getByRole('button', { name: 'Save company' }).click()
  await expect(page.getByRole('status')).toContainText(`Saved Fastlane ${tag}`)

  await page.getByRole('button', { name: 'Add job' }).click()
  const jobForm = page.getByTestId('job-form')
  await jobForm.getByLabel('Company').selectOption({ label: `Fastlane ${tag}` })
  await jobForm.getByLabel('Title').fill(`Founding Engineer ${tag}`)
  await jobForm.getByLabel('Location').fill('Bali (remote ok)')
  await jobForm.getByLabel('Pay').fill('$150K + equity')
  await jobForm.getByLabel('Summary').fill('Build the product with the founders.')
  await jobForm.getByLabel('Description').fill('<h2>What you will do</h2><ul><li>Ship daily</li></ul><script>alert(1)</script>')
  await jobForm.getByRole('button', { name: 'Save job' }).click()
  await expect(page.getByRole('status')).toContainText('Dropped: <script>')
  await expect(page.getByTestId('jobs-table')).toContainText(`Founding Engineer ${tag}`)

  // a board for someone not in the database: created from the picker
  await page.getByRole('link', { name: 'Boards', exact: true }).first().click()
  await page.getByRole('link', { name: 'New board' }).click()
  await page.getByLabel('Person').fill(`Zed ${tag}`)
  await expect(page.getByText('Nobody by that name.')).toBeVisible()
  await page.getByRole('button', { name: 'Create a person' }).click()
  await page.getByTestId('create-person').getByLabel('Email').fill(`zed-${tag}@example.com`)
  await page.getByRole('button', { name: 'Create and use' }).click()
  await expect(page.getByTestId('picked-person')).toContainText(`Zed ${tag}`)
  const personId = (await page.getByTestId('picked-person').getByRole('link', { name: 'record' }).getAttribute('href'))!.split('/').pop()!
  await page.getByLabel('Filter jobs').fill(tag)
  await page.getByRole('button', { name: new RegExp(`Founding Engineer ${tag}`) }).click()
  await expect(page.getByTestId('selected-jobs')).toContainText(`Founding Engineer ${tag}`)
  await page.getByRole('button', { name: 'Create board' }).click()
  await expect(page).toHaveURL(/\/boards\/[0-9a-f-]{36}$/) // the editor reloads the new board
  const url = (await page.locator('code').first().textContent())!.trim()
  const boardId = url.split('/').pop()!
  expect(boardId).toMatch(/^[0-9a-f-]{36}$/)

  // the public page, without a session, through the prefix this stack serves it under
  const anon = await context.browser()!.newContext()
  const pub = await anon.newPage()
  await pub.goto(`/glojobs/${boardId}`)
  await expect(pub.getByRole('heading', { level: 1 })).toHaveText('Roles picked for Zed')
  await expect(pub.locator('meta[name="robots"]')).toHaveAttribute('content', 'noindex')
  await expect(pub.getByRole('button', { name: new RegExp(`Fastlane ${tag}`) })).toBeVisible() // the company chip
  await pub.getByRole('link', { name: new RegExp(`Founding Engineer ${tag}`) }).click()
  await expect(pub.getByRole('heading', { level: 1 })).toHaveText(`Founding Engineer ${tag}`)
  await expect(pub.getByText('Ship daily')).toBeVisible()
  await expect(pub.getByText('Fastlane makes ads.')).toBeVisible()
  await expect(pub.getByText('Bali (remote ok)')).toBeVisible()

  // a slug not on this board, and a random board, are "not here"
  const r = await pub.request.get(`/glojobs/${boardId}/roles/nope`)
  expect(r.status()).toBe(404)
  expect((await pub.request.get('/glojobs/00000000-0000-4000-8000-000000000000')).status()).toBe(404)
  await anon.close()

  // the opened count moved and the person's record lists the board
  await page.reload()
  await expect(page.getByText(/Opened 1 time/)).toBeVisible()
  await page.goto(`/people/${personId}`)
  await expect(page.getByTestId('boards-panel')).toContainText('opened 1×')
  await expect(page.getByTestId('boards-panel').locator('code')).toHaveText(url)
})
