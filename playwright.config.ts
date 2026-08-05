import { defineConfig } from "@playwright/test";
import { resolve } from "node:path";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 45_000,
  expect: { timeout: 8_000 },
  use: {
    baseURL: "http://127.0.0.1:3107",
    channel: "msedge",
    locale: "zh-CN",
    permissions: ["clipboard-read", "clipboard-write"],
    trace: "retain-on-failure",
  },
  webServer: {
    command: "npm.cmd run dev -- --hostname 127.0.0.1 --port 3107",
    url: "http://127.0.0.1:3107",
    reuseExistingServer: false,
    timeout: 120_000,
    env: {
      ...process.env,
      PROMPT_BUILDER_DB_PATH: resolve("data", "e2e.db"),
      PROMPT_BUILDER_UPLOAD_ROOT: resolve("data", "e2e-uploads"),
      OPENAI_COMPAT_API_KEY: "",
    },
  },
});
