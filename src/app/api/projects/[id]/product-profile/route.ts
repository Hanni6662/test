import { z } from "zod";
import { productFactsSchema } from "@/domain/types";
import { api, parseBody } from "@/server/http";
import { getPromptBuilderService } from "@/server/persistence/runtime";

type Context = { params: Promise<{ id: string }> };
const profileSchema = z.object({
  facts: productFactsSchema,
  referenceImages: z.array(z.object({
    identifier: z.string().trim().min(1).max(500),
    filename: z.string().trim().min(1).max(255),
  })).max(50),
});

export async function GET(_request: Request, context: Context) {
  return api(async () => getPromptBuilderService().getProductProfile((await context.params).id));
}

export async function POST(request: Request, context: Context) {
  return api(async () => getPromptBuilderService().upsertProductProfile(
    (await context.params).id,
    await parseBody(request, profileSchema),
  ));
}

