import { z } from "zod";

export const moduleCategories = [
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
] as const;

export const moduleCategorySchema = z.enum(moduleCategories);
export type ModuleCategory = z.infer<typeof moduleCategorySchema>;

export const moduleCapabilities = [
  "background:white",
  "scene:environment",
  "text:none",
  "text:title",
  "view:back",
] as const;

export const moduleCapabilitySchema = z.enum(moduleCapabilities);
export type ModuleCapability = z.infer<typeof moduleCapabilitySchema>;

const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f]/g;

function normalizedText(maxLength: number) {
  return z
    .string()
    .max(maxLength)
    .transform((value) =>
      value.normalize("NFKC").replace(CONTROL_CHARACTERS, " ").replace(/\s+/g, " ").trim(),
    )
    .pipe(z.string().min(1).max(maxLength));
}

export const moduleIdSchema = z.string().regex(
  /^[a-z][a-z0-9_]{2,63}$/,
  "Module IDs must be 3-64 lowercase ASCII letters, digits, or underscores",
);

export const productFactsSchema = z.object({
  title: normalizedText(200),
  category: normalizedText(120),
  audience: normalizedText(120),
  material: normalizedText(240),
  colors: z.array(normalizedText(80)).max(20),
  features: z.array(normalizedText(240)).max(50),
  backViewEvidence: z.boolean().default(false),
});

export type ProductFacts = z.infer<typeof productFactsSchema>;

export const promptModuleDefinitionSchema = z.object({
  id: moduleIdSchema,
  label: normalizedText(120),
  category: moduleCategorySchema,
  content: normalizedText(2000),
  capabilities: z.array(moduleCapabilitySchema).max(moduleCapabilities.length).default([]),
});

export interface PromptModuleDefinition {
  readonly id: string;
  readonly label: string;
  readonly category: ModuleCategory;
  readonly content: string;
  readonly capabilities?: readonly ModuleCapability[];
}

export const imageDesignSnapshotSchema = z.object({
  facts: productFactsSchema,
  selectedModuleIds: z
    .array(moduleIdSchema)
    .max(200)
    .refine((ids) => new Set(ids).size === ids.length, "Selected module IDs must be unique"),
  customModules: z.array(promptModuleDefinitionSchema).default([]),
});

export interface ImageDesignSnapshot {
  readonly facts: ProductFacts;
  readonly selectedModuleIds: readonly string[];
  readonly customModules: readonly PromptModuleDefinition[];
}

export interface CompileIssue {
  code: string;
  message: string;
  moduleIds: string[];
}

export interface PromptSection {
  title: string;
  content: string;
}

export interface CompileResult {
  prompt: string;
  sections: PromptSection[];
  warnings: CompileIssue[];
  errors: CompileIssue[];
}

export type AiProvider = "openai" | "google" | "stability" | "custom";

export interface GenerationRequest {
  provider: AiProvider;
  model: string;
  prompt: string;
  negativePrompt?: string;
  width: number;
  height: number;
  seed?: number;
}

export interface GenerationResult {
  provider: AiProvider;
  requestId: string;
  imageUrls: string[];
}

export interface ImageProviderAdapter {
  readonly provider: AiProvider;
  generate(request: GenerationRequest): Promise<GenerationResult>;
}
