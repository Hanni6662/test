import { defineConfig } from '@playwright/test';
const port = Number(process.env.WORKBENCH_TEST_PORT || 5174);
export default defineConfig({
  testDir: './tests', testMatch: 'creation-isolation.spec.js', timeout: 90000, workers: 1,
  use: { baseURL: `http://127.0.0.1:${port}`, channel: 'chrome', headless: true,
    viewport: { width: 1440, height: 1000 }, trace: 'retain-on-failure' },
  reporter: [['list']],
  webServer: { command: 'node tests/start-chatflow.js', url: `http://127.0.0.1:${port}`,
    reuseExistingServer: false, timeout: 30000 },
});
