import { describe, expect, it } from "vitest";
import { seedPromptModules } from "./seed-modules";
import { promptModuleDefinitionSchema } from "./types";

describe("seedPromptModules", () => {
  it("covers every supported module category", () => {
    const categories = new Set(seedPromptModules.map((module) => module.category));

    expect(categories).toEqual(
      new Set([
        "purpose",
        "subject",
        "composition",
        "camera",
        "scene",
        "style",
        "color",
        "lighting",
        "text",
        "output",
        "negative",
      ]),
    );
  });

  it("includes curated children's apparel commerce scenarios", () => {
    const ids = seedPromptModules.map((module) => module.id);

    expect(ids).toEqual(
      expect.arrayContaining([
        "purpose_amazon_aplus",
        "purpose_amazon_listing",
        "purpose_temu",
        "subject_real_child_model",
        "subject_flat_lay",
        "scene_white_background",
      ]),
    );
  });

  it("is schema-valid, globally unique, and deeply immutable", () => {
    expect(() => promptModuleDefinitionSchema.array().parse(seedPromptModules)).not.toThrow();
    const ids = seedPromptModules.map((module) => module.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(Object.isFrozen(seedPromptModules)).toBe(true);
    expect(seedPromptModules.every((module) => Object.isFrozen(module))).toBe(true);
    expect(
      seedPromptModules.every(
        (module) => !module.capabilities || Object.isFrozen(module.capabilities),
      ),
    ).toBe(true);
    expect(
      seedPromptModules.every(
        (module) => !module.exportableParameterKeys || Object.isFrozen(module.exportableParameterKeys),
      ),
    ).toBe(true);
  });

  it("provides explicit Chinese semantics for every curated module", () => {
    expect(seedPromptModules.every((module) => /[\u3400-\u9fff]/u.test(module.contentZh))).toBe(true);
    expect(seedPromptModules.every((module) => module.contentZh !== module.content)).toBe(true);
  });
});
