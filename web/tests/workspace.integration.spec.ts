import { expect, test } from '@playwright/test';

test.skip(!process.env.BENCHMAXXING_E2E_URL, 'Set BENCHMAXXING_E2E_URL to a running, prepared local service.');

test('real Docker workflow passes the correct submission and rejects the incorrect one', async ({ page }) => {
  async function startAndInspectRun() {
    const created = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/v1/runs');
    await page.getByRole('button', { name: 'Start benchmark' }).click();
    const response = await created;
    expect(response.status()).toBe(201);
    const run = await response.json() as { id: string };
    await expect(page.locator('.run-summary-title > .mono')).toHaveText(run.id.slice(0, 12));
    return run.id;
  }
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'Start benchmark' })).toBeEnabled();
  await page.getByLabel('Participant', { exact: true }).selectOption('scripted');
  await page.getByLabel('Reference behavior').selectOption('good');
  const goodId = await startAndInspectRun();
  await expect(page.locator('.run-summary .status')).toHaveText('passed', { timeout: 90_000 });
  await page.getByRole('tab', { name: 'Grading' }).click();
  await expect(page.locator('.score-number')).toHaveText('100%');
  await page.getByRole('tab', { name: 'Artifacts' }).click();
  await page.getByRole('button', { name: /solution.py/ }).click();
  await expect(page.locator('.artifact-preview pre')).toContainText('merge_intervals');
  const goodExport = await page.getByRole('link', { name: 'Export', exact: true }).getAttribute('href');
  expect(goodExport).toBe(`/api/v1/runs/${goodId}/export`);
  const exportResponse = await page.request.get(goodExport!);
  expect(exportResponse.status()).toBe(200);
  expect(exportResponse.headers()['content-type']).toContain('application/zip');
  expect((await exportResponse.body()).subarray(0, 2).toString()).toBe('PK');
  await page.getByLabel('Reference behavior').selectOption('bad');
  await startAndInspectRun();
  await expect(page.locator('.run-summary .status')).toHaveText('failed', { timeout: 90_000 });
  await page.getByRole('tab', { name: 'Grading' }).click();
  await expect(page.locator('.check-icon.fail').first()).toBeVisible();
});
