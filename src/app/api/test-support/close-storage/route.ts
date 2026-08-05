import { closeRuntimeService } from "@/server/persistence/runtime";

export async function POST(request: Request) {
  const token = process.env.PROMPT_BUILDER_E2E_SHUTDOWN_TOKEN;
  if (!token || request.headers.get("authorization") !== `Bearer ${token}`) {
    return Response.json({ error: { code: "NOT_FOUND", message: "Not found" } }, { status: 404 });
  }
  closeRuntimeService();
  return new Response(null, { status: 204 });
}
