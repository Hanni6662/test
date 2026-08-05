import { NextResponse } from "next/server";
import { z } from "zod";
import { ServiceError } from "./persistence/service";

export async function parseBody<T>(request: Request, schema: z.ZodType<T>): Promise<T> {
  return schema.parse(await request.json());
}

export async function api<T>(operation: () => T | Promise<T>, status = 200): Promise<NextResponse> {
  try {
    return NextResponse.json(await operation(), { status });
  } catch (error) {
    if (error instanceof z.ZodError) {
      return NextResponse.json({
        error: {
          code: "INVALID_INPUT",
          message: "Request validation failed",
          issues: error.issues.map(({ code, path, message }) => ({ code, path, message })),
        },
      }, { status: 400 });
    }
    if (error instanceof SyntaxError) {
      return NextResponse.json({
        error: { code: "INVALID_JSON", message: "Request body must be valid JSON" },
      }, { status: 400 });
    }
    if (error instanceof ServiceError) {
      return NextResponse.json({
        error: { code: error.code, message: error.message },
      }, { status: error.status });
    }
    return NextResponse.json({
      error: { code: "INTERNAL_ERROR", message: "An unexpected error occurred" },
    }, { status: 500 });
  }
}

