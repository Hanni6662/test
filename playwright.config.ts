import { defineConfig } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { join } from "node:path";
import { createOwnedE2eRoot, isOwnedE2eRoot } from "./e2e/temp-root";

let shutdownToken = process.env.PROMPT_BUILDER_E2E_SHUTDOWN_TOKEN ?? randomUUID();
let e2eRoot = process.env.PROMPT_BUILDER_E2E_ROOT;
if (!isOwnedE2eRoot(e2eRoot, shutdownToken)) {
  shutdownToken = randomUUID();
  e2eRoot = createOwnedE2eRoot(shutdownToken);
}
process.env.PROMPT_BUILDER_E2E_ROOT = e2eRoot;
process.env.PROMPT_BUILDER_E2E_SHUTDOWN_TOKEN = shutdownToken;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 45_000,
  expect: { timeout: 8_000 },
  globalTeardown: "./e2e/global-teardown.ts",
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
      PROMPT_BUILDER_DB_PATH: join(e2eRoot, "app.db"),
      PROMPT_BUILDER_UPLOAD_ROOT: join(e2eRoot, "uploads"),
      PROMPT_BUILDER_E2E_ROOT: e2eRoot,
      PROMPT_BUILDER_E2E_SHUTDOWN_TOKEN: shutdownToken,
      OPENAI_COMPAT_API_KEY: "",
    },
  },
});
