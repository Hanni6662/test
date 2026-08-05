import {
  imageDesignSnapshotSchema,
  promptModuleDefinitionSchema,
  type CompileIssue,
  type CompileResult,
  type ImageDesignSnapshot,
  type ModuleCategory,
  type ModuleCapability,
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
  if (categoryDifference) return categoryDifference;
  return left.id < right.id ? -1 : left.id > right.id ? 1 : 0;
}

function issue(code: string, message: string, moduleIds: string[]): CompileIssue {
  return {
    code,
    message,
    moduleIds: [...new Set(moduleIds)].sort((left, right) =>
      left < right ? -1 : left > right ? 1 : 0,
    ),
  };
}

function modulesWithCapability(
  modules: readonly PromptModuleDefinition[],
  capability: ModuleCapability,
): PromptModuleDefinition[] {
  return modules.filter((module) => module.capabilities?.includes(capability));
}

function assertUniqueModuleIds(modules: readonly PromptModuleDefinition[]): void {
  const seen = new Set<string>();
  for (const definition of modules) {
    if (seen.has(definition.id)) throw new Error(`Duplicate module ID: ${definition.id}`);
    seen.add(definition.id);
  }
}

export function compilePrompt(
  snapshotInput: ImageDesignSnapshot,
  catalogInput: readonly PromptModuleDefinition[],
): CompileResult {
  const snapshot = imageDesignSnapshotSchema.parse(snapshotInput);
  const catalog = promptModuleDefinitionSchema.array().parse(catalogInput);
  const allModules = [...catalog, ...snapshot.customModules];
  assertUniqueModuleIds(allModules);

  const selectedIds = new Set(snapshot.selectedModuleIds);
  const knownIds = new Set(allModules.map((module) => module.id));
  const selectedModules = [
    ...catalog.filter((module) => selectedIds.has(module.id)),
    ...snapshot.customModules,
  ].sort(byCanonicalOrder);

  const warnings: CompileIssue[] = [];
  const errors: CompileIssue[] = [];
  const unknownIds = [...selectedIds].filter((id) => !knownIds.has(id));
  if (unknownIds.length) {
    errors.push(
      issue(
        "UNKNOWN_MODULE_ID",
        "Every selected module ID must resolve to a catalog or custom module.",
        unknownIds,
      ),
    );
  }

  const whiteBackgroundModules = modulesWithCapability(selectedModules, "background:white");
  const environmentModules = modulesWithCapability(selectedModules, "scene:environment");

  if (whiteBackgroundModules.length && environmentModules.length) {
    warnings.push(
      issue(
        "WHITE_BACKGROUND_WITH_SCENE",
        "Pure white background conflicts with an environmental scene; choose one visual direction.",
        [...whiteBackgroundModules, ...environmentModules].map((module) => module.id),
      ),
    );
  }

  const noTextModules = modulesWithCapability(selectedModules, "text:none");
  const titleTextModules = modulesWithCapability(selectedModules, "text:title");
  if (noTextModules.length && titleTextModules.length) {
    warnings.push(
      issue(
        "NO_TEXT_WITH_TITLE",
        "No-text output conflicts with the selected title headline.",
        [...noTextModules, ...titleTextModules].map((module) => module.id),
      ),
    );
  }

  const backViewModules = modulesWithCapability(selectedModules, "view:back");
  if (backViewModules.length && !snapshot.facts.backViewEvidence) {
    errors.push(
      issue(
        "BACK_VIEW_EVIDENCE_REQUIRED",
        "A back view cannot be generated without verified back-view product evidence.",
        backViewModules.map((module) => module.id),
      ),
    );
  }

  const sections = SECTION_TITLES.map((title, index) => {
    if (index === 0) return { title, content: formatFacts(snapshot.facts) };

    const categories = new Set(SECTION_CATEGORIES[index]);
    const content = selectedModules
      .filter((module) => categories.has(module.category))
      .map((module) => `- ${module.content}`)
      .join("\n");
    return { title, content: content || "No additional direction selected." };
  });

  const prompt = sections.map((section) => `## ${section.title}\n${section.content}`).join("\n\n");
  return { prompt, sections, warnings, errors };
}
