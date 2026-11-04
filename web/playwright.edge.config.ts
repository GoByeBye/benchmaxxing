import { defineConfig } from '@playwright/test';
import base from './playwright.config';

// Optional local fallback when the installed Playwright Chromium is unavailable.
export default defineConfig({
  ...base,
  use: { ...base.use, channel: 'msedge' },
});
