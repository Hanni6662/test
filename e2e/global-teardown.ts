import { removeOwnedE2eRoot } from "./temp-root";

export default async function globalTeardown() {
  const root = process.env.PROMPT_BUILDER_E2E_ROOT;
  const token = process.env.PROMPT_BUILDER_E2E_SHUTDOWN_TOKEN;
  try {
    if (token) await fetch("http://127.0.0.1:3107/api/test-support/close-storage", { method: "POST", headers: { authorization: `Bearer ${token}` } });
  } finally {
    removeOwnedE2eRoot(root, token);
  }
}
