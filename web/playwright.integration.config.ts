import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  testMatch: '**/*.integration.spec.ts',
  workers: 1,
  timeout: 180_000,
  reporter: 'list',
  use: {
    baseURL: process.env.BENCHMAXXING_E2E_URL || 'http://127.0.0.1:8123',
    trace: 'retain-on-failure',
    ...devices['Desktop Chrome'],
  },
});
