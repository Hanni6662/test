import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { ProductFacts } from "@/domain/types";
import { ServiceError, createPromptBuilderService, type PromptBuilderService } from "./service";

const facts: ProductFacts = {
  title: "Blue cotton dress",
  category: "Children's dresses",
  audience: "Girls age 6-8",
  material: "100% cotton",
  colors: ["blue"],
  features: ["embroidered collar"],
  backViewEvidence: false,
};

describe("PromptBuilderService persistence", () => {
  let directory: string;
  let service: PromptBuilderService;

  beforeEach(() => {
    directory = mkdtempSync(join(tmpdir(), "prompt-builder-"));
    service = createPromptBuilderService(join(directory, "app.db"));
  });

  afterEach(() => {
    service.close();
    rmSync(directory, { recursive: true, force: true });
  });

  it("persists project renames, duplicates, product profiles, and design autosaves", () => {
    const project = service.createProject({ name: "Summer launch" });
    service.renameProject(project.id, { name: "Summer launch 2026" });
    service.upsertProductProfile(project.id, {
      facts,
      referenceImages: [{ identifier: "sha256:front", filename: "front.png" }],
    });
    const design = service.createDesign(project.id, {
      name: "Hero",
      selectedModuleIds: ["purpose_amazon_listing"],
      parameters: { width: 1024, height: 1024 },
      custom: { safeArea: "10%" },
    });
    service.autosaveDesign(design.id, {
      selectedModuleIds: ["purpose_amazon_listing", "lighting_soft_daylight"],
      parameters: { width: 1600, height: 1600 },
    });
    service.close();
    service = createPromptBuilderService(join(directory, "app.db"));

    expect(service.getDesign(design.id).parameters).toEqual({ width: 1600, height: 1600 });
    expect(service.getProductProfile(project.id).referenceImages[0]?.identifier).toBe("sha256:front");
    expect(service.listProjects()[0]?.name).toBe("Summer launch 2026");

    const duplicate = service.duplicateProject(project.id, { name: "Copy" });
    expect(service.getProductProfile(duplicate.id).facts.title).toBe(facts.title);
    expect(service.listProjects()).toHaveLength(2);
  });

  it("seeds builtins and refuses to mutate them", () => {
    const builtin = service.listModules().find((module) => module.kind === "builtin");
    expect(builtin).toBeDefined();
    expect(() =>
      service.updateCustomModule(builtin!.id, { content: "tampered" }),
    ).toThrowError(new ServiceError("BUILTIN_IMMUTABLE", 409, "Builtin modules cannot be modified"));
  });

  it("supports the full custom-module lifecycle and cloning a builtin", () => {
    const custom = service.createCustomModule({
      id: "custom_summer_scene",
      label: "Summer scene",
      category: "scene",
      content: "Use a restrained sunny backdrop.",
      capabilities: ["scene:environment"],
    });
    expect(custom.version).toBe(1);

    const updated = service.updateCustomModule(custom.id, { content: "Use soft summer sunlight." });
    expect(updated.version).toBe(2);
    service.disableModule(custom.id);
    expect(service.listModules({ includeDisabled: true }).find((module) => module.id === custom.id)?.enabled).toBe(false);

    const clone = service.cloneBuiltinModule("lighting_soft_daylight", {
      id: "custom_soft_daylight",
      label: "My soft daylight",
    });
    expect(clone.kind).toBe("custom");
    expect(clone.definition.content).toContain("diffused daylight");
  });

  it("keeps saved version snapshots immutable when profiles and modules change", () => {
    const project = service.createProject({ name: "Snapshots" });
    service.upsertProductProfile(project.id, { facts, referenceImages: [] });
    const customModule = service.createCustomModule({
      id: "custom_snapshot_light",
      label: "Snapshot light",
      category: "lighting",
      content: "Original lighting instruction.",
      capabilities: [],
    });
    const design = service.createDesign(project.id, {
      name: "Snapshot design",
      selectedModuleIds: [customModule.id],
      parameters: { quality: "high" },
      custom: { notes: "initial" },
    });
    const saved = service.saveVersion(design.id);

    service.upsertProductProfile(project.id, {
      facts: { ...facts, title: "Changed product" },
      referenceImages: [],
    });
    service.updateCustomModule(customModule.id, { content: "Changed lighting instruction." });

    const historical = service.listVersions(design.id)[0]!;
    expect(historical.id).toBe(saved.id);
    expect(historical.productSnapshot.facts.title).toBe("Blue cotton dress");
    expect(historical.moduleSnapshot[0]?.definition.content).toBe("Original lighting instruction.");
    expect(historical.compileSnapshot.prompt).toContain("Original lighting instruction.");
  });

  it("restores by creating a new immutable version and leaves originals intact", () => {
    const project = service.createProject({ name: "Restore" });
    service.upsertProductProfile(project.id, { facts, referenceImages: [] });
    const design = service.createDesign(project.id, {
      name: "Restore design",
      selectedModuleIds: ["purpose_amazon_listing"],
      parameters: { width: 1000 },
      custom: {},
    });
    const original = service.saveVersion(design.id);
    service.autosaveDesign(design.id, { parameters: { width: 2000 } });

    const restored = service.restoreVersion(design.id, original.id);

    expect(restored.id).not.toBe(original.id);
    expect(restored.versionNumber).toBe(2);
    expect(restored.designSnapshot.parameters).toEqual({ width: 1000 });
    expect(service.listVersions(design.id).map((version) => version.id)).toEqual([
      restored.id,
      original.id,
    ]);
  });

  it("exports a stable, safe versioned JSON document", () => {
    const project = service.createProject({ name: "Export" });
    service.upsertProductProfile(project.id, {
      facts,
      referenceImages: [{ identifier: "sha256:front", filename: "front.png" }],
    });
    const design = service.createDesign(project.id, {
      name: "Export design",
      selectedModuleIds: ["purpose_amazon_listing"],
      parameters: { width: 1024, apiKey: "must-not-export" },
      custom: { headline: "Summer" },
    });
    const version = service.saveVersion(design.id);

    const exported = service.exportVersion(version.id);
    const json = JSON.stringify(exported);

    expect(exported).toMatchObject({
      formatVersion: 1,
      facts,
      referenceImageIdentifiers: ["sha256:front"],
      parameters: { width: 1024 },
      custom: { headline: "Summer" },
      compilerVersion: "1.0.0",
    });
    expect(exported.compile).toEqual(version.compileSnapshot);
    expect(exported.errors).toEqual(version.compileSnapshot.errors);
    expect(exported.warnings).toEqual(version.compileSnapshot.warnings);
    expect(json).not.toContain("must-not-export");
    expect(json.toLowerCase()).not.toContain("apikey");
    expect(json).not.toContain("imageData");
  });
});
