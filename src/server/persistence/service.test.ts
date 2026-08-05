import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import BetterSqlite3 from "better-sqlite3";
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
  let databasePath: string;
  let service: PromptBuilderService;

  beforeEach(() => {
    directory = mkdtempSync(join(tmpdir(), "prompt-builder-"));
    databasePath = join(directory, "app.db");
    service = createPromptBuilderService(databasePath);
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

  it("rolls back a profile and its reference images when replacement insertion fails", () => {
    const project = service.createProject({ name: "Profile rollback" });
    service.upsertProductProfile(project.id, {
      facts,
      referenceImages: [{ identifier: "sha256:old", filename: "old.png" }],
    });
    const injector = new BetterSqlite3(databasePath);
    injector.exec(`CREATE TRIGGER fail_reference_insert
      BEFORE INSERT ON reference_images BEGIN SELECT RAISE(ABORT, 'injected failure'); END`);
    injector.close();

    expect(() => service.upsertProductProfile(project.id, {
      facts: { ...facts, title: "Must roll back" },
      referenceImages: [{ identifier: "sha256:new", filename: "new.png" }],
    })).toThrow("injected failure");

    expect(service.getProductProfile(project.id)).toMatchObject({
      facts: { title: facts.title },
      referenceImages: [{ identifier: "sha256:old", filename: "old.png" }],
    });
  });

  it("rolls back a duplicated project when copying child rows fails", () => {
    const source = service.createProject({ name: "Duplicate rollback" });
    service.upsertProductProfile(source.id, {
      facts,
      referenceImages: [{ identifier: "sha256:source", filename: "source.png" }],
    });
    const injector = new BetterSqlite3(databasePath);
    injector.exec(`CREATE TRIGGER fail_duplicate_reference
      BEFORE INSERT ON reference_images WHEN NEW.project_id <> '${source.id}'
      BEGIN SELECT RAISE(ABORT, 'injected duplicate failure'); END`);
    injector.close();

    expect(() => service.duplicateProject(source.id, { name: "Partial copy" }))
      .toThrow("injected duplicate failure");
    expect(service.listProjects().map((project) => project.name)).toEqual(["Duplicate rollback"]);
  });

  it("rolls back a restored draft when inserting its new version fails", () => {
    const project = service.createProject({ name: "Restore rollback" });
    service.upsertProductProfile(project.id, { facts, referenceImages: [] });
    const design = service.createDesign(project.id, {
      name: "Restore rollback design",
      selectedModuleIds: ["purpose_amazon_listing"],
      parameters: { width: 1000 },
      custom: {},
    });
    const original = service.saveVersion(design.id);
    service.autosaveDesign(design.id, { parameters: { width: 2000 } });
    const injector = new BetterSqlite3(databasePath);
    injector.exec(`CREATE TRIGGER fail_restore_version
      BEFORE INSERT ON prompt_versions BEGIN SELECT RAISE(ABORT, 'injected restore failure'); END`);
    injector.close();

    expect(() => service.restoreVersion(design.id, original.id)).toThrow("injected restore failure");
    expect(service.getDesign(design.id).parameters).toEqual({ width: 2000 });
    expect(service.listVersions(design.id)).toHaveLength(1);
  });

  it("compiles a saved version directly from the snapshots captured for that version", () => {
    const project = service.createProject({ name: "Atomic capture" });
    service.upsertProductProfile(project.id, { facts, referenceImages: [] });
    const design = service.createDesign(project.id, {
      name: "Atomic design",
      selectedModuleIds: ["purpose_amazon_listing"],
      parameters: {},
      custom: {},
    });
    service.compile = () => {
      throw new Error("saveVersion must not call the rereading compile method");
    };

    const version = service.saveVersion(design.id);

    expect(version.productSnapshot.facts.title).toBe(facts.title);
    expect(version.compileSnapshot.prompt).toContain(`Title: ${facts.title}`);
  });

  it("allocates distinct continuous version numbers across service instances", async () => {
    const project = service.createProject({ name: "Concurrent versions" });
    service.upsertProductProfile(project.id, { facts, referenceImages: [] });
    const design = service.createDesign(project.id, {
      name: "Concurrent design",
      selectedModuleIds: ["purpose_amazon_listing"],
      parameters: {},
      custom: {},
    });
    const secondService = createPromptBuilderService(databasePath);
    try {
      const versions = await Promise.all([
        Promise.resolve().then(() => service.saveVersion(design.id)),
        Promise.resolve().then(() => secondService.saveVersion(design.id)),
      ]);
      expect(versions.map((version) => version.versionNumber).sort()).toEqual([1, 2]);
      for (const version of versions) {
        expect(version.compileSnapshot.prompt).toContain(version.productSnapshot.facts.title);
        for (const promptModule of version.moduleSnapshot) {
          expect(version.compileSnapshot.prompt).toContain(promptModule.definition.content);
        }
      }
    } finally {
      secondService.close();
    }
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
      parameters: {
        width: 1024,
        apiKey: "must-not-export",
        clientSecret: "client-secret-value",
        authorizationHeader: "bearer secret",
        apiSecretKey: "api-secret-key-value",
        passwordHash: "password-hash-value",
        secretValue: "secret-value",
        sessionCookie: "session-cookie-value",
        nested: {
          access_token: "access-token-value",
          tokenCount: 37,
          entries: [
            { refreshToken: "refresh-token-value", label: "safe entry" },
            { "private-key": "private-key-value", retryCount: 2 },
          ],
        },
      },
      custom: {
        headline: "Summer",
        credentials: [{ client_secret: "custom-client-secret", caption: "safe caption" }],
      },
    });
    const version = service.saveVersion(design.id);

    const exported = service.exportVersion(version.id);
    const json = JSON.stringify(exported);

    expect(exported).toMatchObject({
      formatVersion: 1,
      facts,
      referenceImageIdentifiers: ["sha256:front"],
      parameters: {
        width: 1024,
        nested: {
          tokenCount: 37,
          entries: [
            { label: "safe entry" },
            { retryCount: 2 },
          ],
        },
      },
      custom: {
        headline: "Summer",
        credentials: [{ caption: "safe caption" }],
      },
      compilerVersion: "1.0.0",
    });
    expect(exported.compile).toEqual(version.compileSnapshot);
    expect(exported.errors).toEqual(version.compileSnapshot.errors);
    expect(exported.warnings).toEqual(version.compileSnapshot.warnings);
    expect(json).not.toContain("must-not-export");
    expect(json.toLowerCase()).not.toContain("apikey");
    expect(json).not.toContain("client-secret-value");
    expect(json).not.toContain("access-token-value");
    expect(json).not.toContain("refresh-token-value");
    expect(json).not.toContain("private-key-value");
    expect(json).not.toContain("custom-client-secret");
    expect(json).not.toContain("bearer secret");
    expect(json).not.toContain("api-secret-key-value");
    expect(json).not.toContain("password-hash-value");
    expect(json).not.toContain("secret-value");
    expect(json).not.toContain("session-cookie-value");
    expect(json).toContain("tokenCount");
    expect(json).not.toContain("imageData");
  });
});
