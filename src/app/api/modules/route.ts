import { z } from "zod";
import { moduleIdSchema, promptModuleDefinitionSchema } from "@/domain/types";
import { api, parseBody } from "@/server/http";
import { getPromptBuilderService } from "@/server/persistence/runtime";

const createSchema = z.discriminatedUnion("action", [
  z.object({ action: z.literal("create"), module: promptModuleDefinitionSchema }),
  z.object({
    action: z.literal("clone"),
    sourceId: moduleIdSchema,
    id: moduleIdSchema,
    label: z.string().trim().min(1).max(120),
  }),
]);
const patchSchema = z.discriminatedUnion("action", [
  z.object({
    id: moduleIdSchema,
    action: z.literal("update"),
    patch: promptModuleDefinitionSchema.omit({ id: true }).partial(),
  }),
  z.object({ id: moduleIdSchema, action: z.literal("disable") }),
]);

export async function GET(request: Request) {
  return api(() => getPromptBuilderService().listModules({
    includeDisabled: new URL(request.url).searchParams.get("includeDisabled") === "true",
  }));
}

export async function POST(request: Request) {
  return api(async () => {
    const input = await parseBody(request, createSchema);
    return input.action === "create"
      ? getPromptBuilderService().createCustomModule(input.module)
      : getPromptBuilderService().cloneBuiltinModule(input.sourceId, { id: input.id, label: input.label });
  }, 201);
}

export async function PATCH(request: Request) {
  return api(async () => {
    const input = await parseBody(request, patchSchema);
    return input.action === "update"
      ? getPromptBuilderService().updateCustomModule(input.id, input.patch)
      : getPromptBuilderService().disableModule(input.id);
  });
}

