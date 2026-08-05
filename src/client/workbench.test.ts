import { afterEach, describe, expect, it, vi } from "vitest";
import { canExportCompile, createDebouncer, describeCompile, diffVersions, imageClassificationOptions, safeDraftExport, shouldAcceptCompile } from "./workbench";

describe("workbench helpers", () => {
  afterEach(() => vi.useRealTimers());

  it("creates a readable section-level version diff", () => {
    const diff = diffVersions(
      { sections: [{ title: "Scene", content: "White" }, { title: "Text", content: "None" }] },
      { sections: [{ title: "Scene", content: "Garden" }, { title: "Text", content: "None" }, { title: "Output", content: "Square" }] },
    );
    expect(diff).toEqual([
      { title: "Scene", before: "White", after: "Garden", status: "changed" },
      { title: "Output", before: "", after: "Square", status: "added" },
    ]);
  });

  it("describes compile sections and issues in Chinese", () => {
    const text = describeCompile({
      sections: [{ title: "Scene", content: "- Pure white" }],
      errors: [{ code: "BACK_VIEW_EVIDENCE_REQUIRED", message: "missing", moduleIds: [] }],
      warnings: [{ code: "NO_TEXT_WITH_TITLE", message: "conflict", moduleIds: [] }],
    });
    expect(text).toContain("场景：Pure white");
    expect(text).toContain("缺少背面图证据");
    expect(text).toContain("无文字与标题设置冲突");
  });

  it("debounces saves and can cancel pending work", () => {
    vi.useFakeTimers();
    const calls: string[] = [];
    const debounced = createDebouncer((value: string) => calls.push(value), 300);
    debounced.schedule("first");
    debounced.schedule("latest");
    vi.advanceTimersByTime(299);
    expect(calls).toEqual([]);
    vi.advanceTimersByTime(1);
    expect(calls).toEqual(["latest"]);
    debounced.schedule("cancelled");
    debounced.cancel();
    vi.advanceTimersByTime(300);
    expect(calls).toEqual(["latest"]);
  });

  it("closes the hard gate while compile is pending or has errors", () => {
    expect(canExportCompile(null)).toBe(false);
    expect(canExportCompile({ errors: [{ code: "X" }], warnings: [] })).toBe(false);
    expect(canExportCompile({ errors: [], warnings: [{ code: "SOFT" }] })).toBe(true);
  });

  it("exports only declared draft fields and strips secret-shaped values", () => {
    expect(safeDraftExport({
      selectedModuleIds: ["output_amazon_listing"],
      parameters: { width: 1600, apiKey: "secret", metadata: { label: "ok", access_token: "hidden", bearerCredential: "hidden", cookieJar: "hidden" }, height: 900 },
      custom: { scene: "White space", aiPolishedPrompt: "internal" },
      allowedParameterKeys: ["width", "metadata"],
      compile: { prompt: "safe" },
    })).toEqual({
      formatVersion: 1,
      selectedModuleIds: ["output_amazon_listing"],
      parameters: { width: 1600, metadata: { label: "ok" } },
      custom: { scene: "White space" },
      compile: { prompt: "safe" },
    });
    expect(imageClassificationOptions).toContain("variant");
    expect(imageClassificationOptions).not.toContain("packaging");
  });

  it("accepts compile responses only for the latest edit revision", () => {
    expect(shouldAcceptCompile(4, 4)).toBe(true);
    expect(shouldAcceptCompile(3, 4)).toBe(false);
  });
});
