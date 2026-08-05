import { api, parseBody } from "@/server/http";
import { getPromptBuilderService } from "@/server/persistence/runtime";
import { productProfileWriteSchema } from "@/server/persistence/service";

type Context = { params: Promise<{ id: string }> };
export async function GET(_request: Request, context: Context) {
  return api(async () => getPromptBuilderService().getProductProfile((await context.params).id));
}

export async function POST(request: Request, context: Context) {
  return api(async () => getPromptBuilderService().upsertProductProfile(
    (await context.params).id,
    await parseBody(request, productProfileWriteSchema),
  ));
}
