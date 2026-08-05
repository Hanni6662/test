import { z } from "zod";
import { moduleIdSchema } from "@/domain/types";
import { api, parseBody } from "@/server/http";
import { getPromptBuilderService } from "@/server/persistence/runtime";

type Context = { params: Promise<{ id: string }> };
const patchSchema = z.object({
  name: z.string().trim().min(1).max(200).optional(),
  selectedModuleIds: z.array(moduleIdSchema).max(200).optional(),
  parameters: z.record(z.string(), z.json()).optional(),
  custom: z.record(z.string(), z.json()).optional(),
});

export async function GET(_request: Request, context: Context) {
  return api(async () => getPromptBuilderService().getDesign((await context.params).id));
}

export async function PATCH(request: Request, context: Context) {
  return api(async () => getPromptBuilderService().autosaveDesign(
    (await context.params).id,
    await parseBody(request, patchSchema),
  ));
}

