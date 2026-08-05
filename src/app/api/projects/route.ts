import { z } from "zod";
import { api, parseBody } from "@/server/http";
import { getPromptBuilderService } from "@/server/persistence/runtime";

const createSchema = z.object({ name: z.string().trim().min(1).max(200) });

export async function GET() {
  return api(() => getPromptBuilderService().listProjects());
}

export async function POST(request: Request) {
  return api(async () => getPromptBuilderService().createProject(await parseBody(request, createSchema)), 201);
}

