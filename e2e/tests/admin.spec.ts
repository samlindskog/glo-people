import { expect, test } from '@playwright/test'
import { READER, WRITER } from '../users'
import { login, uid } from './helpers'

test('API key works against /mcp and stops working once revoked', async ({ page, request }) => {
  await login(page, WRITER)
  await page.getByRole('link', { name: 'Accounts' }).click()
  const name = `e2e ${uid()}`
  await page.getByLabel('Key name').fill(name)
  await page.getByRole('button', { name: 'Issue key' }).click()
  const key = (await page.getByTestId('new-key-value').textContent())!.trim()
  expect(key).toMatch(/^glo_/)

  const rpc = (method: string, params = {}) =>
    request.post('/mcp', {
      headers: { Authorization: `Bearer ${key}`, Accept: 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-06-18' },
      data: { jsonrpc: '2.0', id: 1, method, params },
    })
  expect((await rpc('initialize', { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'e2e', version: '0' } })).ok()).toBe(true)
  const tools = await (await rpc('tools/list')).json()
  const names = tools.result.tools.map((t: { name: string }) => t.name)
  expect(names).toEqual(expect.arrayContaining(['search_people', 'get_person', 'list_properties', 'create_person']))
  const res = await (await rpc('tools/call', { name: 'search_people', arguments: { filters: [{ field: 'name', op: 'contains', value: 'a' }], limit: 1 } })).json()
  expect(res.result.isError ?? false).toBe(false)

  const row = page.getByTestId('keys-table').getByRole('row', { name: new RegExp(name) })
  page.once('dialog', (d) => d.accept())
  await row.getByRole('button', { name: 'Revoke' }).click()
  await expect(row).toContainText('revoked')
  expect((await rpc('tools/list')).status()).toBe(401)
})

test('reader can manage readers but not grant writer; activity shows it', async ({ page }) => {
  await login(page, READER)
  await page.getByRole('link', { name: 'Accounts' }).click()
  const roleSelect = page.getByLabel('New role')
  await expect(roleSelect.locator('option')).toHaveText(['Reader'])
  const username = `e2e-new-${uid()}`
  await page.getByLabel('New username').fill(username)
  await page.getByLabel('New password').fill('a-long-enough-password')
  await page.getByRole('button', { name: 'Create account' }).click()
  await expect(page.getByTestId('users-table')).toContainText(username)
  // writer rows are not manageable by a reader
  const writerRow = page.getByTestId('users-table').getByRole('row', { name: new RegExp(WRITER.username) })
  await expect(writerRow.getByRole('button', { name: 'Deactivate' })).toHaveCount(0)

  await page.getByRole('link', { name: 'History' }).click()
  await page.getByRole('tab', { name: 'Activity' }).click()
  await expect(page.getByTestId('activity-table')).toContainText('user.create')
  await expect(page.getByTestId('activity-table')).toContainText(username)
})
