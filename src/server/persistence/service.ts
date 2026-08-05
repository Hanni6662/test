import { randomUUID } from "node:crypto";
import { mkdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import BetterSqlite3 from "better-sqlite3";
import { desc, eq, sql } from "drizzle-orm";
import { drizzle } from "drizzle-orm/better-sqlite3";
import { z } from "zod";
import { compilePrompt } from "@/domain/prompt-compiler";
import { seedPromptModules } from "@/domain/seed-modules";
import {
  moduleIdSchema,
  moduleCategories,
  productFactsSchema,
  promptModuleDefinitionSchema,
  type CompileResult,
  type ModuleCategory,
  type ProductFacts,
  type PromptModuleDefinition,
} from "@/domain/types";
import {
  imageDesigns,
  productProfiles,
  projects,
  promptModules,
  promptVersions,
  referenceImages,
} from "./schema";

export const COMPILER_VERSION = "1.0.0";
const BUILTIN_MODULE_VERSION = 2;

const nameSchema = z.string().trim().min(1).max(200);
const referenceImageSchema = z.object({
  identifier: z.string().trim().min(1).max(500),
  filename: z.string().trim().min(1).max(255),
});
const productProfileInputSchema = z.object({
  facts: productFactsSchema,
  referenceImages: z.array(referenceImageSchema).max(50),
});
const jsonObjectSchema = z.record(z.string(), z.json());
const selectedModuleIdsSchema = z.array(moduleIdSchema).max(200).refine(
  (ids) => new Set(ids).size === ids.length,
  "Selected module IDs must be unique",
);
export const designCreateSchema = z.object({
  name: nameSchema,
  selectedModuleIds: selectedModuleIdsSchema.default([]),
  parameters: jsonObjectSchema.default({}),
  custom: jsonObjectSchema.default({}),
});
export const designPatchSchema = z.object({
  name: nameSchema.optional(),
  selectedModuleIds: selectedModuleIdsSchema.optional(),
  parameters: jsonObjectSchema.optional(),
  custom: jsonObjectSchema.optional(),
});
const compileIssueSchema = z.object({
  code: z.string(),
  message: z.string(),
  moduleIds: z.array(z.string()),
});
const compileResultSchema = z.object({
  prompt: z.string(),
  sections: z.array(z.object({ title: z.string(), content: z.string() })),
  warnings: z.array(compileIssueSchema),
  errors: z.array(compileIssueSchema),
});
const storedModuleSchema = z.object({
  kind: z.enum(["builtin", "custom"]),
  version: z.number().int().positive(),
  definition: promptModuleDefinitionSchema,
});
const productSnapshotSchema = productProfileInputSchema;
const designSnapshotSchema = designCreateSchema.extend({ id: z.string(), projectId: z.string() });

export type ReferenceImage = z.infer<typeof referenceImageSchema>;
export type ProductProfile = z.infer<typeof productProfileInputSchema>;
export type DesignSnapshot = z.infer<typeof designSnapshotSchema>;
export type StoredModule = z.infer<typeof storedModuleSchema> & { id: string; enabled: boolean };
export type ModuleVersionSnapshot = z.infer<typeof storedModuleSchema>;

export interface Project {
  id: string;
  name: string;
  createdAt: string;
  updatedAt: string;
}

export interface PromptVersion {
  id: string;
  designId: string;
  versionNumber: number;
  productSnapshot: ProductProfile;
  moduleSnapshot: ModuleVersionSnapshot[];
  designSnapshot: DesignSnapshot;
  compileSnapshot: CompileResult;
  compilerVersion: string;
  createdAt: string;
}

export interface VersionExport {
  formatVersion: 1;
  facts: ProductFacts;
  referenceImageIdentifiers: string[];
  modules: ModuleVersionSnapshot[];
  parameters: Record<string, z.infer<typeof z.json>>;
  custom: Partial<Record<ModuleCategory, string>>;
  compile: CompileResult;
  errors: CompileResult["errors"];
  warnings: CompileResult["warnings"];
  timestamp: string;
  compilerVersion: string;
}

export class ServiceError extends Error {
  constructor(
    public readonly code: string,
    public readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ServiceError";
  }
}

function now(): string {
  return new Date().toISOString();
}

function parseJson<T>(value: string, schema: z.ZodType<T>): T {
  return schema.parse(JSON.parse(value));
}

function safeParameters(
  value: Record<string, z.infer<typeof z.json>>,
  allowedKeys: ReadonlySet<string>,
): Record<string, z.infer<typeof z.json>> {
  const isSensitiveKey = (key: string): boolean => {
    const normalized = key.toLowerCase().replace(/[^a-z0-9]/g, "");
    return normalized.includes("authorization")
      || normalized.includes("authheader")
      || normalized.includes("apikey")
      || normalized.includes("bearercredential")
      || normalized.includes("privatekey")
      || normalized.includes("password")
      || normalized.includes("secret")
      || normalized.includes("sessionid")
      || normalized.includes("cookiejar")
      || normalized.endsWith("token")
      || normalized.endsWith("cookie");
  };
  const clean = (input: z.infer<typeof z.json>): z.infer<typeof z.json> => {
    if (Array.isArray(input)) return input.map(clean);
    if (input && typeof input === "object") {
      return Object.fromEntries(
        Object.entries(input)
          .filter(([key]) => !isSensitiveKey(key))
          .map(([key, nested]) => [key, clean(nested)]),
      );
    }
    return input;
  };
  return Object.fromEntries(
    Object.entries(value)
      .filter(([key]) => allowedKeys.has(key) && !isSensitiveKey(key))
      .map(([key, nested]) => [key, clean(nested)]),
  );
}

function safeCustomDirections(
  value: Record<string, z.infer<typeof z.json>>,
): Partial<Record<ModuleCategory, string>> {
  const categories = new Set<string>(moduleCategories);
  return Object.fromEntries(
    Object.entries(value).filter(([key, direction]) => categories.has(key) && typeof direction === "string"),
  );
}

export class PromptBuilderService {
  private readonly sqlite: BetterSqlite3.Database;
  private readonly db: ReturnType<typeof drizzle>;

  constructor(databasePath: string) {
    mkdirSync(dirname(databasePath), { recursive: true });
    this.sqlite = new BetterSqlite3(databasePath);
    this.sqlite.pragma("foreign_keys = ON");
    this.sqlite.pragma("busy_timeout = 5000");
    this.sqlite.exec(readFileSync(join(process.cwd(), "drizzle", "0000_initial.sql"), "utf8"));
    this.db = drizzle(this.sqlite);
    this.seedModules();
  }

  close(): void {
    this.sqlite.close();
  }

  private atomic<T>(operation: () => T): T {
    return this.sqlite.transaction(operation).immediate();
  }

  private seedModules(): void {
    const timestamp = now();
    this.db.insert(promptModules).values(
      seedPromptModules.map((definition) => ({
        id: definition.id,
        kind: "builtin" as const,
        definitionJson: JSON.stringify(definition),
        version: BUILTIN_MODULE_VERSION,
        enabled: true,
        createdAt: timestamp,
        updatedAt: timestamp,
      })),
    ).onConflictDoUpdate({
      target: promptModules.id,
      set: {
        kind: "builtin",
        definitionJson: sql`excluded.definition_json`,
        version: BUILTIN_MODULE_VERSION,
        enabled: true,
        updatedAt: timestamp,
      },
    }).run();
  }

  createProject(input: { name: string }): Project {
    const name = nameSchema.parse(input.name);
    const timestamp = now();
    const project = { id: randomUUID(), name, createdAt: timestamp, updatedAt: timestamp };
    this.db.insert(projects).values(project).run();
    return project;
  }

  listProjects(): Project[] {
    return this.db.select().from(projects).orderBy(desc(projects.updatedAt)).all();
  }

  getProject(id: string): Project {
    const project = this.db.select().from(projects).where(eq(projects.id, id)).get();
    if (!project) throw new ServiceError("PROJECT_NOT_FOUND", 404, "Project not found");
    return project;
  }

  renameProject(id: string, input: { name: string }): Project {
    this.getProject(id);
    this.db.update(projects).set({ name: nameSchema.parse(input.name), updatedAt: now() }).where(eq(projects.id, id)).run();
    return this.getProject(id);
  }

  duplicateProject(id: string, input?: { name?: string }): Project {
    return this.atomic(() => {
      const source = this.getProject(id);
      const duplicate = this.createProject({ name: input?.name ?? `${source.name} copy` });
      const profile = this.db.select().from(productProfiles).where(eq(productProfiles.projectId, id)).get();
      if (profile) {
        this.upsertProductProfile(duplicate.id, this.getProductProfile(id));
      }
      const designs = this.db.select().from(imageDesigns).where(eq(imageDesigns.projectId, id)).all();
      for (const design of designs) {
        this.createDesign(duplicate.id, {
          name: design.name,
          selectedModuleIds: parseJson(design.selectedModuleIdsJson, selectedModuleIdsSchema),
          parameters: parseJson(design.parametersJson, jsonObjectSchema),
          custom: parseJson(design.customJson, jsonObjectSchema),
        });
      }
      return duplicate;
    });
  }

  upsertProductProfile(projectId: string, input: ProductProfile): ProductProfile {
    const profile = productProfileInputSchema.parse(input);
    return this.atomic(() => {
      this.getProject(projectId);
      const timestamp = now();
      this.db.insert(productProfiles).values({
        projectId,
        factsJson: JSON.stringify(profile.facts),
        updatedAt: timestamp,
      }).onConflictDoUpdate({
        target: productProfiles.projectId,
        set: { factsJson: JSON.stringify(profile.facts), updatedAt: timestamp },
      }).run();
      this.db.delete(referenceImages).where(eq(referenceImages.projectId, projectId)).run();
      if (profile.referenceImages.length) {
        this.db.insert(referenceImages).values(profile.referenceImages.map((image) => ({
          id: randomUUID(),
          projectId,
          identifier: image.identifier,
          filename: image.filename,
          createdAt: timestamp,
        }))).run();
      }
      return this.getProductProfile(projectId);
    });
  }

  getProductProfile(projectId: string): ProductProfile {
    this.getProject(projectId);
    const profile = this.db.select().from(productProfiles).where(eq(productProfiles.projectId, projectId)).get();
    if (!profile) throw new ServiceError("PRODUCT_PROFILE_NOT_FOUND", 404, "Product profile not found");
    return productProfileInputSchema.parse({
      facts: JSON.parse(profile.factsJson),
      referenceImages: this.db.select().from(referenceImages).where(eq(referenceImages.projectId, projectId)).all()
        .map(({ identifier, filename }) => ({ identifier, filename })),
    });
  }

  listModules(options: { includeDisabled?: boolean } = {}): StoredModule[] {
    const rows = options.includeDisabled
      ? this.db.select().from(promptModules).all()
      : this.db.select().from(promptModules).where(eq(promptModules.enabled, true)).all();
    return rows.map((row) => ({
      id: row.id,
      kind: row.kind,
      version: row.version,
      enabled: row.enabled,
      definition: parseJson(row.definitionJson, promptModuleDefinitionSchema),
    }));
  }

  createCustomModule(input: PromptModuleDefinition): StoredModule {
    const definition = promptModuleDefinitionSchema.parse(input);
    const timestamp = now();
    try {
      this.db.insert(promptModules).values({
        id: definition.id,
        kind: "custom",
        definitionJson: JSON.stringify(definition),
        version: 1,
        enabled: true,
        createdAt: timestamp,
        updatedAt: timestamp,
      }).run();
    } catch (error) {
      if (error instanceof BetterSqlite3.SqliteError
        && (error.code === "SQLITE_CONSTRAINT_PRIMARYKEY" || error.code === "SQLITE_CONSTRAINT_UNIQUE")) {
        throw new ServiceError("MODULE_ID_EXISTS", 409, "Module ID already exists");
      }
      throw error;
    }
    return this.getModule(definition.id);
  }

  private getModule(id: string): StoredModule {
    const row = this.db.select().from(promptModules).where(eq(promptModules.id, id)).get();
    if (!row) throw new ServiceError("MODULE_NOT_FOUND", 404, "Module not found");
    return {
      id: row.id,
      kind: row.kind,
      version: row.version,
      enabled: row.enabled,
      definition: parseJson(row.definitionJson, promptModuleDefinitionSchema),
    };
  }

  updateCustomModule(id: string, patch: Partial<Omit<PromptModuleDefinition, "id">>): StoredModule {
    const current = this.getModule(id);
    if (current.kind === "builtin") {
      throw new ServiceError("BUILTIN_IMMUTABLE", 409, "Builtin modules cannot be modified");
    }
    const definition = promptModuleDefinitionSchema.parse({ ...current.definition, ...patch, id });
    this.db.update(promptModules).set({
      definitionJson: JSON.stringify(definition),
      version: current.version + 1,
      updatedAt: now(),
    }).where(eq(promptModules.id, id)).run();
    return this.getModule(id);
  }

  disableModule(id: string): StoredModule {
    const current = this.getModule(id);
    if (current.kind === "builtin") {
      throw new ServiceError("BUILTIN_IMMUTABLE", 409, "Builtin modules cannot be modified");
    }
    this.db.update(promptModules).set({ enabled: false, updatedAt: now() }).where(eq(promptModules.id, id)).run();
    return this.getModule(id);
  }

  cloneBuiltinModule(id: string, input: { id: string; label: string }): StoredModule {
    const source = this.getModule(id);
    if (source.kind !== "builtin") {
      throw new ServiceError("MODULE_NOT_BUILTIN", 409, "Only builtin modules can be cloned");
    }
    return this.createCustomModule({ ...source.definition, id: input.id, label: input.label });
  }

  createDesign(projectId: string, input: z.input<typeof designCreateSchema>): DesignSnapshot {
    this.getProject(projectId);
    const parsed = designCreateSchema.parse(input);
    const timestamp = now();
    const id = randomUUID();
    this.db.insert(imageDesigns).values({
      id,
      projectId,
      name: parsed.name,
      selectedModuleIdsJson: JSON.stringify(parsed.selectedModuleIds),
      parametersJson: JSON.stringify(parsed.parameters),
      customJson: JSON.stringify(parsed.custom),
      createdAt: timestamp,
      updatedAt: timestamp,
    }).run();
    return this.getDesign(id);
  }

  getDesign(id: string): DesignSnapshot {
    const row = this.db.select().from(imageDesigns).where(eq(imageDesigns.id, id)).get();
    if (!row) throw new ServiceError("DESIGN_NOT_FOUND", 404, "Design not found");
    return designSnapshotSchema.parse({
      id: row.id,
      projectId: row.projectId,
      name: row.name,
      selectedModuleIds: JSON.parse(row.selectedModuleIdsJson),
      parameters: JSON.parse(row.parametersJson),
      custom: JSON.parse(row.customJson),
    });
  }

  listDesigns(projectId: string): DesignSnapshot[] {
    this.getProject(projectId);
    return this.db.select({ id: imageDesigns.id }).from(imageDesigns)
      .where(eq(imageDesigns.projectId, projectId)).all().map(({ id }) => this.getDesign(id));
  }

  autosaveDesign(id: string, patch: z.input<typeof designPatchSchema>): DesignSnapshot {
    const current = this.getDesign(id);
    const parsed = designPatchSchema.parse(patch);
    this.db.update(imageDesigns).set({
      name: parsed.name ?? current.name,
      selectedModuleIdsJson: JSON.stringify(parsed.selectedModuleIds ?? current.selectedModuleIds),
      parametersJson: JSON.stringify(parsed.parameters ?? current.parameters),
      customJson: JSON.stringify(parsed.custom ?? current.custom),
      updatedAt: now(),
    }).where(eq(imageDesigns.id, id)).run();
    return this.getDesign(id);
  }

  compile(id: string): CompileResult {
    const design = this.getDesign(id);
    const profile = this.getProductProfile(design.projectId);
    const modules = this.listModules().map(({ kind, version, definition }) => ({ kind, version, definition }));
    return this.compileSnapshots(design, profile, modules);
  }

  private compileSnapshots(
    design: DesignSnapshot,
    profile: ProductProfile,
    modules: ModuleVersionSnapshot[],
  ): CompileResult {
    return compileResultSchema.parse(compilePrompt({
      facts: profile.facts,
      selectedModuleIds: design.selectedModuleIds,
      customModules: [],
    }, modules.map((module) => module.definition)));
  }

  saveVersion(designId: string): PromptVersion {
    return this.atomic(() => {
      const design = this.getDesign(designId);
      const product = this.getProductProfile(design.projectId);
      const selected = new Set(design.selectedModuleIds);
      const modules = this.listModules()
        .filter((module) => selected.has(module.definition.id))
        .map(({ kind, version, definition }) => ({ kind, version, definition }));
      const compileSnapshot = this.compileSnapshots(design, product, modules);
      const latest = this.db.select().from(promptVersions).where(eq(promptVersions.designId, designId))
        .orderBy(desc(promptVersions.versionNumber)).get();
      return this.insertVersion({
        designId,
        versionNumber: (latest?.versionNumber ?? 0) + 1,
        productSnapshot: product,
        moduleSnapshot: modules,
        designSnapshot: design,
        compileSnapshot,
      });
    });
  }

  private insertVersion(input: Omit<PromptVersion, "id" | "compilerVersion" | "createdAt">): PromptVersion {
    const id = randomUUID();
    const createdAt = now();
    this.db.insert(promptVersions).values({
      id,
      designId: input.designId,
      versionNumber: input.versionNumber,
      productSnapshotJson: JSON.stringify(productSnapshotSchema.parse(input.productSnapshot)),
      moduleSnapshotJson: JSON.stringify(storedModuleSchema.array().parse(input.moduleSnapshot)),
      designSnapshotJson: JSON.stringify(designSnapshotSchema.parse(input.designSnapshot)),
      compileSnapshotJson: JSON.stringify(compileResultSchema.parse(input.compileSnapshot)),
      compilerVersion: COMPILER_VERSION,
      createdAt,
    }).run();
    return this.getVersion(id);
  }

  private getVersion(id: string): PromptVersion {
    const row = this.db.select().from(promptVersions).where(eq(promptVersions.id, id)).get();
    if (!row) throw new ServiceError("VERSION_NOT_FOUND", 404, "Version not found");
    return {
      id: row.id,
      designId: row.designId,
      versionNumber: row.versionNumber,
      productSnapshot: parseJson(row.productSnapshotJson, productSnapshotSchema),
      moduleSnapshot: parseJson(row.moduleSnapshotJson, storedModuleSchema.array()),
      designSnapshot: parseJson(row.designSnapshotJson, designSnapshotSchema),
      compileSnapshot: parseJson(row.compileSnapshotJson, compileResultSchema),
      compilerVersion: row.compilerVersion,
      createdAt: row.createdAt,
    };
  }

  listVersions(designId: string): PromptVersion[] {
    this.getDesign(designId);
    return this.db.select({ id: promptVersions.id }).from(promptVersions)
      .where(eq(promptVersions.designId, designId)).orderBy(desc(promptVersions.versionNumber)).all()
      .map(({ id }) => this.getVersion(id));
  }

  restoreVersion(designId: string, versionId: string): PromptVersion {
    return this.atomic(() => {
      this.getDesign(designId);
      const source = this.getVersion(versionId);
      if (source.designId !== designId) {
        throw new ServiceError("VERSION_NOT_FOUND", 404, "Version not found for design");
      }
      this.autosaveDesign(designId, {
        name: source.designSnapshot.name,
        selectedModuleIds: source.designSnapshot.selectedModuleIds,
        parameters: source.designSnapshot.parameters,
        custom: source.designSnapshot.custom,
      });
      const latest = this.listVersions(designId)[0];
      return this.insertVersion({
        designId,
        versionNumber: (latest?.versionNumber ?? 0) + 1,
        productSnapshot: source.productSnapshot,
        moduleSnapshot: source.moduleSnapshot,
        designSnapshot: { ...source.designSnapshot, id: designId },
        compileSnapshot: source.compileSnapshot,
      });
    });
  }

  exportVersion(versionId: string): VersionExport {
    const version = this.getVersion(versionId);
    const compile = {
      prompt: version.compileSnapshot.prompt,
      sections: version.compileSnapshot.sections.map(({ title, content }) => ({ title, content })),
      warnings: version.compileSnapshot.warnings.map(({ code, message, moduleIds }) => ({
        code, message, moduleIds: [...moduleIds],
      })),
      errors: version.compileSnapshot.errors.map(({ code, message, moduleIds }) => ({
        code, message, moduleIds: [...moduleIds],
      })),
    };
    return {
      formatVersion: 1,
      facts: {
        title: version.productSnapshot.facts.title,
        category: version.productSnapshot.facts.category,
        audience: version.productSnapshot.facts.audience,
        material: version.productSnapshot.facts.material,
        colors: [...version.productSnapshot.facts.colors],
        features: [...version.productSnapshot.facts.features],
        backViewEvidence: version.productSnapshot.facts.backViewEvidence,
      },
      referenceImageIdentifiers: version.productSnapshot.referenceImages.map((image) => image.identifier),
      modules: version.moduleSnapshot.map(({ kind, version: moduleVersion, definition }) => ({
        kind,
        version: moduleVersion,
        definition: {
          id: definition.id,
          label: definition.label,
          category: definition.category,
          content: definition.content,
          capabilities: [...(definition.capabilities ?? [])],
          exportableParameterKeys: [...(definition.exportableParameterKeys ?? [])],
        },
      })),
      parameters: safeParameters(
        version.designSnapshot.parameters,
        new Set(version.moduleSnapshot.flatMap(({ definition }) => definition.exportableParameterKeys ?? [])),
      ),
      custom: safeCustomDirections(version.designSnapshot.custom),
      compile,
      errors: compile.errors,
      warnings: compile.warnings,
      timestamp: version.createdAt,
      compilerVersion: version.compilerVersion,
    };
  }
}

export function createPromptBuilderService(databasePath: string): PromptBuilderService {
  return new PromptBuilderService(databasePath);
}
