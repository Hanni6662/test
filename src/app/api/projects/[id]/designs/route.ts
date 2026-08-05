import { api, parseBody } from "@/server/http";
import { getPromptBuilderService } from "@/server/persistence/runtime";
import { designCreateSchema } from "@/server/persistence/service";

type Context = { params: Promise<{ id: string }> };
export async function GET(_request: Request, context: Context) {
  return api(async () => getPromptBuilderService().listDesigns((await context.params).id));
}

export async function POST(request: Request, context: Context) {
  return api(async () => getPromptBuilderService().createDesign(
    (await context.params).id,
    await parseBody(request, designCreateSchema),
  ), 201);
}
