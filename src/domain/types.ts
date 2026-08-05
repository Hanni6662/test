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

export const productFactsSchema = z.object({
  title: z.string().min(1),
  category: z.string().min(1),
  audience: z.string().min(1),
  material: z.string().min(1),
  colors: z.array(z.string().min(1)),
  features: z.array(z.string().min(1)),
  backViewEvidence: z.boolean().default(false),
});

export type ProductFacts = z.infer<typeof productFactsSchema>;

export const promptModuleDefinitionSchema = z.object({
  id: z.string().min(1),
  label: z.string().min(1),
  category: moduleCategorySchema,
  content: z.string().min(1),
});

export type PromptModuleDefinition = z.infer<typeof promptModuleDefinitionSchema>;

export const imageDesignSnapshotSchema = z.object({
  facts: productFactsSchema,
  selectedModuleIds: z.array(z.string()),
  customModules: z.array(promptModuleDefinitionSchema).default([]),
});

export type ImageDesignSnapshot = z.infer<typeof imageDesignSnapshotSchema>;

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

