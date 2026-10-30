import { expect, test } from '@playwright/test'
import { READER, WRITER } from '../users'
import { login, uid } from './helpers'

// Without FIRECRAWL_API_KEY (CI) the item fails fast with a clear error; with real keys (a local
// dev stack) the made-up profile is simply not found. Either way the run must finish 1/1.
test('writer starts an enrichment run for one person; reader can only watch', async ({ page, browser }) => {
  const tag = uid()
  await login(page, WRITER)
  await page.goto('/people/new')
  await page.getByRole('textbox', { name: 'name' }).fill(`Wren ${tag}`)
  await page.getByRole('button', { name: '+ Add field' }).click()
  await page.getByLabel('New field key').fill('linkedin')
  await page.getByRole('textbox', { name: 'linkedin' }).fill(`https://www.linkedin.com/in/wren-${tag}`)
  await page.getByRole('button', { name: 'Save' }).click()
  await expect(page.getByRole('heading', { name: `Wren ${tag}` })).toBeVisible()

  await page.getByRole('link', { name: 'Workflows' }).click()
  const card = page.getByTestId('workflow-enrich')
  await expect(card).toContainText('Enrich from LinkedIn and the web')
  await card.getByLabel('Person name').fill(`Wren ${tag}`)
  await card.getByRole('option', { name: new RegExp(`Wren ${tag}`) }).click()
  await card.getByRole('button', { name: 'Run' }).click()

  const detail = page.getByTestId('run-detail')
  const items = detail.getByTestId('run-items')
  await expect(items).toContainText(/FIRECRAWL_API_KEY is not set|could not resolve this profile|no data provider/, { timeout: 60_000 })
  await expect(items.locator('tbody tr').first()).toContainText(/done|failed/)
  await expect(page.getByTestId('runs-table')).toContainText('1/1')

  const readerCtx = await browser.newContext()
  const rp = await readerCtx.newPage()
  await login(rp, READER)
  await rp.getByRole('link', { name: 'Workflows' }).click()
  await expect(rp.getByTestId('workflow-enrich')).toBeVisible()
  await expect(rp.getByRole('button', { name: 'Run' })).toHaveCount(0)
  await expect(rp.getByTestId('runs-table')).toContainText('enrich')
  await readerCtx.close()
})
