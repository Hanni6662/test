import { z } from "zod";
import { moduleIdSchema } from "@/domain/types";
import { api, parseBody } from "@/server/http";
import { getPromptBuilderService } from "@/server/persistence/runtime";

type Context = { params: Promise<{ id: string }> };
const designSchema = z.object({
  name: z.string().trim().min(1).max(200),
  selectedModuleIds: z.array(moduleIdSchema).max(200).default([]),
  parameters: z.record(z.string(), z.json()).default({}),
  custom: z.record(z.string(), z.json()).default({}),
});

export async function GET(_request: Request, context: Context) {
  return api(async () => getPromptBuilderService().listDesigns((await context.params).id));
}

export async function POST(request: Request, context: Context) {
  return api(async () => getPromptBuilderService().createDesign(
    (await context.params).id,
    await parseBody(request, designSchema),
  ), 201);
}

