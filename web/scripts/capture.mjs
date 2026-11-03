import { chromium } from 'playwright';
import { mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';

const output = resolve(process.env.BENCHMAXXING_SCREENSHOT_DIR || '../work/ui-review');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ channel: 'chromium', args: ['--disable-gpu', '--force-device-scale-factor=1'] });
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 }, deviceScaleFactor: 1 });
  await page.goto(process.env.BENCHMAXXING_E2E_URL || 'http://127.0.0.1:8123');
  await page.getByRole('button', { name: 'Start benchmark' }).waitFor();
  await page.getByRole('button', { name: /Scripted · good/ }).first().waitFor({ timeout: 5000 }).catch(() => {});
  const good = page.getByRole('button', { name: /Scripted · good/ }).first();
  if (await good.count()) {
    await good.click();
    await page.waitForFunction(() => document.querySelector('.run-summary h3')?.textContent === 'Scripted · good');
    await page.getByRole('tab', { name: 'Grading' }).click();
    await page.locator('.score-number').waitFor();
  }
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: resolve(output, 'desktop.png'), fullPage: true, animations: 'disabled' });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => window.scrollTo(0, 0));
  if (await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)) throw new Error('The mobile layout overflows the viewport.');
  await page.screenshot({ path: resolve(output, 'mobile.png'), fullPage: true, animations: 'disabled' });
  process.stdout.write(`${output}\n`);
} finally {
  await browser.close();
}
