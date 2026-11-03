import { expect, test } from '@playwright/test';

test.skip(!process.env.BENCHMAXXING_E2E_URL, 'Set BENCHMAXXING_E2E_URL to a running, prepared local service.');

test('real Docker workflow passes the correct submission and rejects the incorrect one', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'Start benchmark' })).toBeEnabled();
  await page.getByLabel('Participant', { exact: true }).selectOption('scripted');
  await page.getByLabel('Reference behavior').selectOption('good');
  await page.getByRole('button', { name: 'Start benchmark' }).click();
  await expect(page.locator('.run-summary .status')).toHaveText('passed', { timeout: 90_000 });
  await page.getByRole('tab', { name: 'Grading' }).click();
  await expect(page.locator('.score-number')).toHaveText('100%');
  await page.getByRole('tab', { name: 'Artifacts' }).click();
  await page.getByRole('button', { name: /solution.py/ }).click();
  await expect(page.locator('.artifact-preview pre')).toContainText('merge_intervals');
  const goodExport = await page.getByRole('link', { name: 'Export', exact: true }).getAttribute('href');
  expect(goodExport).toMatch(/\/api\/v1\/runs\/[^/]+\/export$/);
  const exportResponse = await page.request.get(goodExport!);
  expect(exportResponse.status()).toBe(200);
  expect(exportResponse.headers()['content-type']).toContain('application/zip');
  expect((await exportResponse.body()).subarray(0, 2).toString()).toBe('PK');
  await page.getByLabel('Reference behavior').selectOption('bad');
  await page.getByRole('button', { name: 'Start benchmark' }).click();
  await expect(page.locator('.run-summary .status')).toHaveText('failed', { timeout: 90_000 });
  await page.getByRole('tab', { name: 'Grading' }).click();
  await expect(page.locator('.check-icon.fail').first()).toBeVisible();
});
