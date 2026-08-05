import { z } from "zod";
import { productFactsSchema } from "@/domain/types";
import { api, parseBody } from "@/server/http";
import { getPromptBuilderService } from "@/server/persistence/runtime";
type Context = { params: Promise<{ id: string }> };
const schema = z.object({ candidateIds: z.array(z.string().uuid()).max(100), removeCandidateIds: z.array(z.string().uuid()).max(100).optional(), edits: productFactsSchema.partial().optional() }).strict();
export async function POST(request: Request, context: Context) { return api(async () => getPromptBuilderService().confirmProductCandidates((await context.params).id, await parseBody(request, schema))); }
