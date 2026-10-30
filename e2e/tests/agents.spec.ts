import { expect, test } from '@playwright/test'
import { WRITER } from '../users'
import { login, uid } from './helpers'

// These flows call the pydantic-ai agents. They run only where the stack uses the deterministic
// fakes (FAKE_AGENTS=1: CI, or a dev stack started that way); a stack on real models skips them.
test.beforeEach(async ({ request }) => {
  const health = await (await request.get('/api/health')).json()
  test.skip(!health.fake_agents, 'FAKE_AGENTS is off: agent flows would call real models')
})

test('ask plans a search, answers with citations, and the plan runs as a search', async ({ page }) => {
  const tag = uid()
  await login(page, WRITER)
  await page.goto('/people/new')
  await page.getByRole('textbox', { name: 'name' }).fill(`Ask ${tag}`)
  await page.getByRole('button', { name: '+ Add property' }).click()
  await page.getByLabel('New property').last().selectOption('bio')
  await page.getByRole('textbox', { name: 'bio' }).fill(`payments infrastructure ${tag}`)
  await page.getByRole('button', { name: '+ Add property' }).click()
  await page.getByLabel('New property').last().selectOption('mrr')
  await page.getByRole('textbox', { name: 'mrr' }).fill('$25,000')
  await page.getByRole('button', { name: 'Save' }).click()
  await expect(page.getByRole('heading', { name: `Ask ${tag}` })).toBeVisible()
  await expect(page.getByTestId('normalised')).toContainText('25000')

  await page.getByRole('link', { name: 'People', exact: true }).click()
  await page.getByLabel('Question').fill(`${tag} with mrr over 20000`)
  // the fake embedder scores unrelated texts near zero: accept any similarity
  await page.getByRole('region', { name: 'Ask' }).getByText('Advanced').click()
  await page.getByLabel('Ask threshold').fill('0')
  await page.getByRole('button', { name: 'Ask' }).click()
  await expect(page.getByTestId('ask-answer')).toContainText(`Ask ${tag}`)
  await expect(page.getByRole('list', { name: 'Citations' })).toContainText(`Ask ${tag}`)
  await expect(page.getByTestId('ask-plan')).toContainText('mrr ≥ 20000')
  await page.getByRole('button', { name: 'Run as search' }).click()
  await expect(page.getByTestId('result-summary')).toContainText('mrr ≥ 20000')
  await expect(page.getByTestId('results')).toContainText(`Ask ${tag}`)
  // the planned search is in the history, marked as an ask
  await page.getByRole('link', { name: 'My searches' }).click()
  await expect(page.getByTestId('searches-table').getByRole('row', { name: /ask/ }).first()).toContainText('mrr ≥ 20000')
})

test('storing a passage files statements under properties; normalise reports', async ({ page }) => {
  const tag = uid()
  await login(page, WRITER)
  await page.goto('/people/new')
  await page.getByRole('textbox', { name: 'name' }).fill(`Store ${tag}`)
  await page.getByRole('button', { name: 'Save' }).click()
  await expect(page.getByRole('heading', { name: `Store ${tag}` })).toBeVisible()

  await page.getByRole('textbox', { name: 'Passage' }).fill(`notes: met at the summit ${tag}. stage: seed round closed`)
  await page.getByRole('button', { name: 'Store' }).click()
  const report = page.getByTestId('store-report')
  await expect(report.getByRole('row')).toHaveCount(3)
  await expect(report).toContainText('notes')
  await expect(report).toContainText('stage')
  await expect(page.getByText(`met at the summit ${tag}`).first()).toBeVisible()
  // the parser cannot read "seed round closed" as a stage: flagged, not normalised
  await expect(page.getByTestId('normalised')).toContainText('not recognised')
  await expect(page.getByText('history (2)')).toHaveCount(0) // one entry each: nothing overwritten

  // the normaliser agent maps it onto the canonical value
  await page.getByRole('button', { name: 'Normalise' }).click()
  await expect(page.getByTestId('normalise-report')).toContainText('stage → Seed')
  await expect(page.getByTestId('normalised')).toContainText('Seed')

  // the Properties page can normalise everyone who has a property
  await page.getByRole('link', { name: 'Properties' }).click()
  await page.getByRole('button', { name: 'Normalise stage' }).click()
  await expect(page.getByTestId('normalise-started')).toContainText('started')
})
