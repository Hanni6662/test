import { resolve } from "node:path";
import { createPromptBuilderService, type PromptBuilderService } from "./service";

let service: PromptBuilderService | undefined;

export function getPromptBuilderService(): PromptBuilderService {
  if (!service) {
    const databasePath = process.env.PROMPT_BUILDER_DB_PATH ?? resolve(process.cwd(), "data", "app.db");
    service = createPromptBuilderService(databasePath);
  }
  return service;
}

export function closeRuntimeService(): void {
  service?.close();
  service = undefined;
}

