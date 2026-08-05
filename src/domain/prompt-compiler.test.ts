import { describe, expect, it } from "vitest";
import { compilePrompt, SECTION_TITLES } from "./prompt-compiler";
import { seedPromptModules } from "./seed-modules";
import type { ImageDesignSnapshot, PromptModuleDefinition } from "./types";

const facts = {
  title: "Girls' Summer Floral Dress",
  category: "children's dress",
  audience: "girls aged 4-8",
  material: "breathable cotton blend",
  colors: ["ivory", "coral"],
  features: ["flutter sleeves", "floral print"],
  backViewEvidence: false,
};

function snapshot(
  selectedModuleIds: string[],
  customModules: PromptModuleDefinition[] = [],
): ImageDesignSnapshot {
  return { facts, selectedModuleIds, customModules };
}

describe("compilePrompt", () => {
  it("is deterministic regardless of module selection order", () => {
    const first = compilePrompt(
      snapshot(["output_amazon_listing", "purpose_amazon_listing", "scene_white_background"]),
      seedPromptModules,
    );
    const second = compilePrompt(
      snapshot(["scene_white_background", "purpose_amazon_listing", "output_amazon_listing"]),
      seedPromptModules,
    );

    expect(first).toEqual(second);
  });

  it("always emits the fixed eight sections in order", () => {
    const result = compilePrompt(snapshot([]), seedPromptModules);

    expect(result.sections.map((section) => section.title)).toEqual(SECTION_TITLES);
    expect(result.prompt.match(/^## /gm)).toHaveLength(8);
  });

  it("places custom content in the section mapped from its category", () => {
    const custom: PromptModuleDefinition = {
      id: "custom_lighting_softbox",
      label: "Custom softbox",
      category: "lighting",
      content: "Use a large diffused softbox from camera left.",
    };

    const result = compilePrompt(snapshot([], [custom]), seedPromptModules);
    const lookSection = result.sections.find((section) => section.title === "Style, Color & Lighting");

    expect(lookSection?.content).toContain(custom.content);
    expect(result.sections.filter((section) => section.content.includes(custom.content))).toHaveLength(1);
  });

  it("reports a soft conflict between white background and scene modules", () => {
    const result = compilePrompt(
      snapshot(["scene_white_background", "scene_garden_party"]),
      seedPromptModules,
    );

    expect(result.warnings).toContainEqual(
      expect.objectContaining({ code: "WHITE_BACKGROUND_WITH_SCENE" }),
    );
    expect(result.errors).toHaveLength(0);
  });

  it("reports a soft conflict between no-text and title text", () => {
    const result = compilePrompt(
      snapshot(["text_no_text", "text_title_headline"]),
      seedPromptModules,
    );

    expect(result.warnings).toContainEqual(expect.objectContaining({ code: "NO_TEXT_WITH_TITLE" }));
  });

  it("reports a hard error when back view is selected without product evidence", () => {
    const result = compilePrompt(snapshot(["camera_back_view"]), seedPromptModules);

    expect(result.errors).toContainEqual(expect.objectContaining({ code: "BACK_VIEW_EVIDENCE_REQUIRED" }));
  });

  it("never omits the product facts section", () => {
    const result = compilePrompt(snapshot(["purpose_amazon_aplus"]), seedPromptModules);
    const productFacts = result.sections[0];

    expect(productFacts.title).toBe("Product Facts (Immutable)");
    expect(productFacts.content).toContain(facts.title);
    expect(productFacts.content).toContain(facts.material);
    expect(productFacts.content).toContain("flutter sleeves");
  });
});

