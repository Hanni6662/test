import {
  imageDesignSnapshotSchema,
  promptModuleDefinitionSchema,
  type CompileIssue,
  type CompileResult,
  type ImageDesignSnapshot,
  type ModuleCategory,
  type ProductFacts,
  type PromptModuleDefinition,
} from "./types";

export const SECTION_TITLES = [
  "Product Facts (Immutable)",
  "Purpose & Subject",
  "Composition & Camera",
  "Scene",
  "Style, Color & Lighting",
  "Text",
  "Output Specification",
  "Negative Constraints",
] as const;

const SECTION_CATEGORIES: ReadonlyArray<readonly ModuleCategory[]> = [
  [],
  ["purpose", "subject"],
  ["composition", "camera"],
  ["scene"],
  ["style", "color", "lighting"],
  ["text"],
  ["output"],
  ["negative"],
];

const CATEGORY_ORDER = new Map(
  SECTION_CATEGORIES.flat().map((category, index) => [category, index]),
);

function formatFacts(facts: ProductFacts): string {
  return [
    `Title: ${facts.title}`,
    `Category: ${facts.category}`,
    `Audience: ${facts.audience}`,
    `Material: ${facts.material}`,
    `Colors: ${facts.colors.join(", ") || "not supplied"}`,
    `Verified features: ${facts.features.join(", ") || "none supplied"}`,
    `Back-view evidence: ${facts.backViewEvidence ? "supplied" : "not supplied"}`,
    "Treat every fact above as immutable; never invent unsupported product details.",
  ].join("\n");
}

function byCanonicalOrder(left: PromptModuleDefinition, right: PromptModuleDefinition): number {
  const categoryDifference =
    (CATEGORY_ORDER.get(left.category) ?? Number.MAX_SAFE_INTEGER) -
    (CATEGORY_ORDER.get(right.category) ?? Number.MAX_SAFE_INTEGER);
  return categoryDifference || left.id.localeCompare(right.id, "en");
}

function issue(code: string, message: string, moduleIds: string[]): CompileIssue {
  return { code, message, moduleIds: [...moduleIds].sort() };
}

export function compilePrompt(
  snapshotInput: ImageDesignSnapshot,
  catalogInput: readonly PromptModuleDefinition[],
): CompileResult {
  const snapshot = imageDesignSnapshotSchema.parse(snapshotInput);
  const catalog = promptModuleDefinitionSchema.array().parse(catalogInput);
  const selectedIds = new Set(snapshot.selectedModuleIds);
  const selectedModules = [
    ...catalog.filter((module) => selectedIds.has(module.id)),
    ...snapshot.customModules,
  ].sort(byCanonicalOrder);

  const warnings: CompileIssue[] = [];
  const errors: CompileIssue[] = [];
  const selectedSceneIds = selectedModules
    .filter((module) => module.category === "scene")
    .map((module) => module.id);

  if (selectedIds.has("scene_white_background") && selectedSceneIds.some((id) => id !== "scene_white_background")) {
    warnings.push(
      issue(
        "WHITE_BACKGROUND_WITH_SCENE",
        "Pure white background conflicts with an environmental scene; choose one visual direction.",
        selectedSceneIds,
      ),
    );
  }

  if (selectedIds.has("text_no_text") && selectedIds.has("text_title_headline")) {
    warnings.push(
      issue(
        "NO_TEXT_WITH_TITLE",
        "No-text output conflicts with the selected title headline.",
        ["text_no_text", "text_title_headline"],
      ),
    );
  }

  if (selectedIds.has("camera_back_view") && !snapshot.facts.backViewEvidence) {
    errors.push(
      issue(
        "BACK_VIEW_EVIDENCE_REQUIRED",
        "A back view cannot be generated without verified back-view product evidence.",
        ["camera_back_view"],
      ),
    );
  }

  const sections = SECTION_TITLES.map((title, index) => {
    if (index === 0) return { title, content: formatFacts(snapshot.facts) };

    const categories = new Set(SECTION_CATEGORIES[index]);
    const content = selectedModules
      .filter((module) => categories.has(module.category))
      .map((module) => module.content)
      .join("\n");
    return { title, content: content || "No additional direction selected." };
  });

  const prompt = sections.map((section) => `## ${section.title}\n${section.content}`).join("\n\n");
  return { prompt, sections, warnings, errors };
}

