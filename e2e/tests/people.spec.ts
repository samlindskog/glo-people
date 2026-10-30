import { expect, test } from '@playwright/test'
import { READER, WRITER } from '../users'
import { login, uid } from './helpers'

test('login is required and wrong passwords are rejected', async ({ page }) => {
  await page.goto('/people')
  await expect(page).toHaveURL(/\/login/)
  await page.getByLabel('Username').fill(WRITER.username)
  await page.getByLabel('Password').fill('wrong-password')
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('alert')).toContainText('Invalid username or password')
})

test('writer imports a CSV with a new column, filters and searches semantically', async ({ page }) => {
  const tag = uid()
  const col = `e2e_col_${tag}`
  const csv = [
    `ID,Name,Bio,One-liner,City,${col}`,
    `${tag}-1,Ada ${tag},payments infrastructure for merchants,fintech payments founder,Lagos,first`,
    `${tag}-2,Bo ${tag},climate venture investor,climate investor,Berlin,`,
    `${tag}-3,Cy ${tag},payments and lending,,Lagos,third`,
  ].join('\n')

  await login(page, WRITER)
  await page.getByRole('link', { name: 'Import CSV' }).click()
  await page.getByLabel('CSV file').setInputFiles({ name: `e2e-${tag}.csv`, mimeType: 'text/csv', buffer: Buffer.from(csv) })
  await expect(page.getByRole('row', { name: new RegExp(col) })).toContainText('new field')
  await page.getByRole('button', { name: /Import 3 rows/ }).click()
  await expect(page.getByTestId('import-result')).toContainText('3 new')
  await expect(page.getByTestId('import-result')).toContainText(col)
  await expect(page.getByTestId('import-result')).toContainText(/done \(\d+ vectors\)/, { timeout: 30_000 })

  // new column shows up in the field registry
  await page.getByRole('link', { name: 'Fields' }).click()
  await expect(page.getByTestId('fields-table')).toContainText(col)

  // literal filter: name contains tag AND city = lagos
  await page.getByRole('link', { name: 'People', exact: true }).click()
  await page.getByRole('button', { name: '+ Add filter' }).click()
  await page.getByLabel('Filter field').selectOption('name')
  await page.getByLabel('Filter value').fill(tag)
  await page.getByRole('button', { name: '+ Add filter' }).click()
  await page.getByLabel('Filter field').nth(1).selectOption('city')
  await page.getByLabel('Filter operator').nth(1).selectOption('eq')
  await page.getByLabel('Filter value').nth(1).fill('lagos')
  await page.getByRole('button', { name: 'Search', exact: true }).click()
  await expect(page.getByTestId('result-summary')).toContainText('2 people')

  // semantic over bio + one_liner, avg, threshold 0 → Ada ranks first
  await page.getByLabel('Semantic search').check()
  await page.getByLabel('Semantic query').fill('fintech payments founder')
  await page.getByRole('group', { name: 'Semantic fields' }).getByRole('button', { name: /^Bio/ }).click()
  await page.getByRole('group', { name: 'Semantic fields' }).getByRole('button', { name: /^One liner/ }).click()
  await page.getByLabel('Threshold', { exact: true }).fill('0')
  await page.getByRole('button', { name: 'Search', exact: true }).click()
  const firstRow = page.getByTestId('results').locator('tbody tr').first()
  await expect(firstRow).toContainText(`Ada ${tag}`)
  await expect(page.getByTestId('results')).toContainText('one_liner')

  // raising the threshold shrinks the result set
  const before = Number((await page.getByTestId('result-summary').textContent())!.match(/(\d+) (people|person)/)![1])
  await page.getByLabel('Threshold', { exact: true }).fill('0.9')
  await page.getByRole('button', { name: 'Search', exact: true }).click()
  await expect(page.getByTestId('result-summary')).toContainText('≥ 0.9')
  await expect
    .poll(async () => Number((await page.getByTestId('result-summary').textContent())?.match(/(\d+) (people|person)/)?.[1] ?? NaN))
    .toBeLessThan(before)

  // search is in history and re-runs
  await page.getByRole('link', { name: 'My searches' }).click()
  await expect(page.getByTestId('searches-table')).toContainText('fintech payments founder')
  await page.getByRole('link', { name: 'Re-run' }).first().click()
  await expect(page.getByLabel('Semantic query')).toHaveValue('fintech payments founder')
})

test('writer edits and deletes a person; reader cannot', async ({ page, browser }) => {
  const tag = uid()
  await login(page, WRITER)
  await page.goto('/people/new')
  await page.getByRole('textbox', { name: 'name' }).fill(`Zed ${tag}`)
  await page.getByRole('button', { name: '+ Add field' }).click()
  await page.getByLabel('New field key').fill('favourite_tool')
  await page.getByRole('textbox', { name: 'favourite_tool' }).fill('lathe')
  await page.getByRole('button', { name: 'Save' }).click()
  await expect(page.getByRole('heading', { name: `Zed ${tag}` })).toBeVisible()
  await expect(page.getByText('lathe')).toBeVisible()
  const url = page.url()

  await page.getByRole('button', { name: 'Edit' }).click()
  await page.getByRole('textbox', { name: 'favourite_tool' }).fill('drill press')
  await page.getByRole('button', { name: 'Save' }).click()
  await expect(page.getByText('drill press').first()).toBeVisible()
  // the earlier value is kept in the field's history
  await page.getByText('history (2)').click()
  await expect(page.getByText('lathe')).toBeVisible()

  const readerCtx = await browser.newContext()
  const rp = await readerCtx.newPage()
  await login(rp, READER)
  await rp.goto(url)
  await expect(rp.getByText('drill press').first()).toBeVisible()
  await expect(rp.getByRole('button', { name: 'Edit' })).toHaveCount(0)
  await expect(rp.getByRole('link', { name: 'Add person' })).toHaveCount(0)
  await rp.goto('/people/new')
  await expect(rp.getByText('Not allowed')).toBeVisible()
  await readerCtx.close()

  page.once('dialog', (d) => d.accept())
  await page.getByRole('button', { name: 'Delete' }).click()
  await expect(page).toHaveURL(/\/people$/)
  await page.goto(url)
  await expect(page.getByRole('alert')).toContainText('person not found')
})
