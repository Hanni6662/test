import { z } from "zod";
import { api, parseBody } from "@/server/http";
import { getPromptBuilderService } from "@/server/persistence/runtime";
const schema = z.object({ baseUrl: z.string().url(), visionModel: z.string().trim().min(1).max(200), textModel: z.string().trim().min(1).max(200) }).strict();
export async function GET() { return api(() => getPromptBuilderService().getAiSettings()); }
export async function PATCH(request: Request) { return api(async () => { getPromptBuilderService().updateAiSettings(await parseBody(request, schema)); return getPromptBuilderService().getAiSettings(); }); }
