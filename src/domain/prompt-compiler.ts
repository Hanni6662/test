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

export const SECTION_TITLES_ZH = [
  "产品事实（不可更改）",
  "用途与主体",
  "构图与镜头",
  "场景",
  "风格、色彩与灯光",
  "文字",
  "输出规格",
  "负向约束",
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

function formatFactsZh(facts: ProductFacts): string {
  return [
    `商品标题：${facts.title}`,
    `商品类别：${facts.category}`,
    `目标人群：${facts.audience}`,
    `材质：${facts.material}`,
    `颜色：${facts.colors.join("、") || "未提供"}`,
    `已验证卖点：${facts.features.join("、") || "未提供"}`,
    `背面证据：${facts.backViewEvidence ? "已提供" : "未提供"}`,
    "以上产品事实均不可更改；不得虚构任何未经证实的产品细节。",
  ].join("\n");
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
  const catalogById = new Map(catalog.map((module) => [module.id, module]));
  const selectedCatalog = snapshot.selectedModuleIds.flatMap((id) => {
    const definition = catalogById.get(id);
    return definition ? [definition] : [];
  });
  const selectedModules = SECTION_CATEGORIES.flatMap((categories) => categories.flatMap((category) => [
    ...selectedCatalog.filter((module) => module.category === category),
    ...snapshot.customModules
      .filter((module) => module.category === category)
      .sort((left, right) => left.id.localeCompare(right.id)),
  ]));

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
    const titleZh = SECTION_TITLES_ZH[index];
    if (index === 0) return { title, titleZh, content: formatFacts(snapshot.facts), contentZh: formatFactsZh(snapshot.facts) };

    const categories = new Set(SECTION_CATEGORIES[index]);
    const sectionModules = selectedModules.filter((module) => categories.has(module.category));
    const content = sectionModules.map((module) => `- ${module.content}`).join("\n");
    const contentZh = sectionModules.map((module) => `- ${module.contentZh}`).join("\n");
    return {
      title,
      titleZh,
      content: content || "No additional direction selected.",
      contentZh: contentZh || "未选择额外方向。",
    };
  });

  const prompt = sections.map((section) => `## ${section.title}\n${section.content}`).join("\n\n");
  const compiledChinese = sections.map((section) => `## ${section.titleZh}\n${section.contentZh}`).join("\n\n");
  return { prompt, compiledChinese, sections, warnings, errors };
}
