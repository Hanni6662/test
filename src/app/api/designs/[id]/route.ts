import { api, parseBody } from "@/server/http";
import { getPromptBuilderService } from "@/server/persistence/runtime";
import { designPatchSchema } from "@/server/persistence/service";

type Context = { params: Promise<{ id: string }> };
export async function GET(_request: Request, context: Context) {
  return api(async () => getPromptBuilderService().getDesign((await context.params).id));
}

export async function PATCH(request: Request, context: Context) {
  return api(async () => getPromptBuilderService().autosaveDesign(
    (await context.params).id,
    await parseBody(request, designPatchSchema),
  ));
}
