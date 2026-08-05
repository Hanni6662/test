import { z } from "zod";
import { api, parseBody } from "@/server/http";
import { getPromptBuilderService } from "@/server/persistence/runtime";

type Context = { params: Promise<{ id: string }> };
const renameSchema = z.object({ name: z.string().trim().min(1).max(200) });
const duplicateSchema = z.object({ name: z.string().trim().min(1).max(200).optional() });

export async function GET(_request: Request, context: Context) {
  return api(async () => getPromptBuilderService().getProject((await context.params).id));
}

export async function PATCH(request: Request, context: Context) {
  return api(async () => getPromptBuilderService().renameProject(
    (await context.params).id,
    await parseBody(request, renameSchema),
  ));
}

export async function POST(request: Request, context: Context) {
  return api(async () => getPromptBuilderService().duplicateProject(
    (await context.params).id,
    await parseBody(request, duplicateSchema),
  ), 201);
}

