import { describe, expect, it } from "vitest";
import { seedPromptModules } from "./seed-modules";

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
});
