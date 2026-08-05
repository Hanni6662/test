import { integer, sqliteTable, text, uniqueIndex } from "drizzle-orm/sqlite-core";

export const projects = sqliteTable("projects", {
  id: text("id").primaryKey(),
  name: text("name").notNull(),
  createdAt: text("created_at").notNull(),
  updatedAt: text("updated_at").notNull(),
});

export const referenceImages = sqliteTable("reference_images", {
  id: text("id").primaryKey(),
  projectId: text("project_id").notNull().references(() => projects.id, { onDelete: "cascade" }),
  identifier: text("identifier").notNull(),
  filename: text("filename").notNull(),
  storagePath: text("storage_path"),
  mimeType: text("mime_type"),
  size: integer("size"),
  aiClassification: text("ai_classification"),
  userClassification: text("user_classification"),
  confidence: integer("confidence"),
  status: text("status").notNull().default("uploaded"),
  createdAt: text("created_at").notNull(),
});

export const productProfiles = sqliteTable("product_profiles", {
  projectId: text("project_id").primaryKey().references(() => projects.id, { onDelete: "cascade" }),
  factsJson: text("facts_json").notNull(),
  aiCandidatesJson: text("ai_candidates_json").notNull().default("[]"),
  updatedAt: text("updated_at").notNull(),
});

export const aiSettings = sqliteTable("ai_settings", {
  id: integer("id").primaryKey(),
  baseUrl: text("base_url").notNull(),
  visionModel: text("vision_model").notNull(),
  textModel: text("text_model").notNull(),
  updatedAt: text("updated_at").notNull(),
});

export const promptModules = sqliteTable("prompt_modules", {
  id: text("id").primaryKey(),
  kind: text("kind", { enum: ["builtin", "custom"] }).notNull(),
  definitionJson: text("definition_json").notNull(),
  version: integer("version").notNull(),
  enabled: integer("enabled", { mode: "boolean" }).notNull(),
  createdAt: text("created_at").notNull(),
  updatedAt: text("updated_at").notNull(),
});

export const imageDesigns = sqliteTable("image_designs", {
  id: text("id").primaryKey(),
  projectId: text("project_id").notNull().references(() => projects.id, { onDelete: "cascade" }),
  name: text("name").notNull(),
  selectedModuleIdsJson: text("selected_module_ids_json").notNull(),
  parametersJson: text("parameters_json").notNull(),
  customJson: text("custom_json").notNull(),
  polishedPrompt: text("polished_prompt"),
  polishedBasePromptHash: text("polished_base_prompt_hash"),
  revision: integer("revision").notNull().default(0),
  createdAt: text("created_at").notNull(),
  updatedAt: text("updated_at").notNull(),
});

export const promptVersions = sqliteTable("prompt_versions", {
  id: text("id").primaryKey(),
  designId: text("design_id").notNull().references(() => imageDesigns.id, { onDelete: "cascade" }),
  versionNumber: integer("version_number").notNull(),
  productSnapshotJson: text("product_snapshot_json").notNull(),
  moduleSnapshotJson: text("module_snapshot_json").notNull(),
  designSnapshotJson: text("design_snapshot_json").notNull(),
  compileSnapshotJson: text("compile_snapshot_json").notNull(),
  polishedPrompt: text("polished_prompt"),
  polishedBasePromptHash: text("polished_base_prompt_hash"),
  compilerVersion: text("compiler_version").notNull(),
  createdAt: text("created_at").notNull(),
}, (table) => [
  uniqueIndex("prompt_versions_design_version_unique").on(table.designId, table.versionNumber),
]);
