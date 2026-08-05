import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createRequire } from "node:module";
import { Worker } from "node:worker_threads";
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
    });
    service.addLegacyReferenceImages(project.id, [{ identifier: "sha256:front", filename: "front.png" }]);
    const design = service.createDesign(project.id, {
      name: "Hero",
      selectedModuleIds: ["purpose_amazon_listing"],
      parameters: { width: 1024, height: 1024 },
      custom: { safeArea: "10%" },
    });
    service.autosaveDesign(design.id, {
      selectedModuleIds: ["purpose_amazon_listing", "lighting_soft_daylight"],
      parameters: { width: 1600, height: 1600 },
    }, design.revision);
    service.close();
    service = createPromptBuilderService(join(directory, "app.db"));

    expect(service.getDesign(design.id).parameters).toEqual({ width: 1600, height: 1600 });
    expect(service.getProductProfile(project.id).referenceImages[0]?.identifier).toBe("sha256:front");
    expect(service.listProjects()[0]?.name).toBe("Summer launch 2026");

    const duplicate = service.duplicateProject(project.id, { name: "Copy" });
    expect(service.getProductProfile(duplicate.id).facts.title).toBe(facts.title);
    expect(service.listProjects()).toHaveLength(2);
  });

  it("atomically saves and compiles with revision CAS across connections", () => {
    const project = service.createProject({ name: "CAS design" });
    service.upsertProductProfile(project.id, { facts });
    const design = service.createDesign(project.id, {
      name: "CAS hero",
      selectedModuleIds: ["purpose_amazon_listing"],
      parameters: {},
      custom: {},
    });
    expect(design.revision).toBe(0);
    expect(service.compileEnvelope(design.id)).toMatchObject({ designId: design.id, revision: 0, design: { id: design.id, revision: 0 } });
    const secondService = createPromptBuilderService(databasePath);
    try {
      const saved = service.saveAndCompile(design.id, {
        expectedRevision: 0,
        selectedModuleIds: ["purpose_amazon_listing", "lighting_soft_daylight"],
        parameters: { width: 1200 },
        custom: {},
      });
      expect(saved).toMatchObject({ designId: design.id, revision: 1, design: { revision: 1, parameters: { width: 1200 } } });
      expect(saved.compile.prompt).toContain("Use soft diffused daylight");
      expect(() => secondService.saveAndCompile(design.id, {
        expectedRevision: 0,
        selectedModuleIds: ["purpose_amazon_listing"],
        parameters: { width: 900 },
        custom: {},
      })).toThrowError(new ServiceError("STALE_REVISION", 409, "Design revision is stale"));
      expect(secondService.getDesign(design.id)).toMatchObject({ revision: 1, parameters: { width: 1200 } });
    } finally {
      secondService.close();
    }
  });

  it("rejects hard-constraint versions without inserting a row", () => {
    const project = service.createProject({ name: "Hard gate" });
    service.upsertProductProfile(project.id, { facts });
    const design = service.createDesign(project.id, {
      name: "Invalid back view",
      selectedModuleIds: ["camera_back_view"],
      parameters: {},
      custom: {},
    });

    expect(() => service.saveVersion(design.id))
      .toThrowError(new ServiceError("HARD_CONSTRAINTS", 422, "Design has hard constraint errors"));
    expect(service.listVersions(design.id)).toEqual([]);
  });

  it("seeds builtins and refuses to mutate them", () => {
    const builtin = service.listModules().find((module) => module.kind === "builtin");
    expect(builtin).toBeDefined();
    expect(() =>
      service.updateCustomModule(builtin!.id, { content: "tampered" }),
    ).toThrowError(new ServiceError("BUILTIN_IMMUTABLE", 409, "Builtin modules cannot be modified"));
  });

  it("includes one custom direction per declared category in compile output", () => {
    const project = service.createProject({ name: "Custom directions" });
    service.upsertProductProfile(project.id, { facts });
    const design = service.createDesign(project.id, {
      name: "Hero",
      selectedModuleIds: ["purpose_amazon_listing"],
      parameters: {},
      custom: { scene: "Keep generous white space around the garment." },
    });

    const result = service.compile(design.id);

    expect(result.sections.find(({ title }) => title === "Scene")?.content)
      .toContain("Keep generous white space around the garment.");
  });

  it("keeps AI candidates evidenced and compiles only manually confirmed facts", () => {
    const project = service.createProject({ name: "Candidate review" });
    service.upsertProductProfile(project.id, { facts });
    const [image] = service.addReferenceImages(project.id, [{ storagePath: `${project.id}/front.png`, originalFilename: "front.png", mimeType: "image/png", size: 99 }]);
    const candidates = service.replaceAiCandidates(project.id, [{ field: "material", value: "linen", confidence: 0.83, evidenceImageIds: [image.id] }]);
    expect(service.getProductProfile(project.id).facts.material).toBe("100% cotton");
    expect(candidates[0].evidenceImageIds).toEqual([image.id]);

    service.confirmProductCandidates(project.id, { candidateIds: [candidates[0].id], edits: { material: "linen blend" } });
    const profile = service.getProductProfile(project.id);
    expect(profile.confirmedFacts!.material).toBe("linen blend");
    expect(profile.facts.material).toBe("linen blend");
    expect(profile.aiCandidates).toEqual([]);
    expect(() => service.confirmProductCandidates(project.id, { candidateIds: ["00000000-0000-4000-8000-000000000000"] }))
      .toThrowError(new ServiceError("CANDIDATE_NOT_FOUND", 400, "Candidate not found for project"));
  });

  it("uses user classification in preference to AI classification", () => {
    const project = service.createProject({ name: "Classification" });
    const [image] = service.addReferenceImages(project.id, [{ storagePath: `${project.id}/x.png`, originalFilename: "x.png", mimeType: "image/png", size: 8 }]);
    service.setAiClassifications(project.id, [{ imageId: image.id, classification: "front", confidence: 0.9 }]);
    service.updateReferenceImage(image.id, { userClassification: "detail" });
    expect(service.listReferenceImages(project.id)[0]).toMatchObject({ aiClassification: "front", userClassification: "detail", effectiveClassification: "detail" });
  });

  it("applies complete AI classification sets atomically without downgrading reviewed images", () => {
    const project = service.createProject({ name: "Atomic classifications" });
    const [first, second] = service.addReferenceImages(project.id, [
      { storagePath: `${project.id}/one.png`, originalFilename: "one.png", mimeType: "image/png", size: 8 },
      { storagePath: `${project.id}/two.png`, originalFilename: "two.png", mimeType: "image/png", size: 8 },
    ]);
    service.setAiClassifications(project.id, [
      { imageId: first.id, classification: "main", confidence: 0.7 },
      { imageId: second.id, classification: "side", confidence: 0.6 },
    ]);
    service.updateReferenceImage(first.id, { userClassification: "detail" });

    expect(() => service.setAiClassifications(project.id, [
      { imageId: first.id, classification: "back", confidence: 0.9 },
      { imageId: "00000000-0000-4000-8000-000000000000", classification: "front", confidence: 0.9 },
    ])).toThrowError(new ServiceError("CLASSIFICATION_SET_INVALID", 400, "Classifications must include every project image exactly once"));
    expect(service.listReferenceImages(project.id).map(({ aiClassification }) => aiClassification)).toEqual(["main", "side"]);
    expect(() => service.setAiClassifications(project.id, [{ imageId: first.id, classification: "front", confidence: 0.8 }]))
      .toThrowError(new ServiceError("CLASSIFICATION_SET_INVALID", 400, "Classifications must include every project image exactly once"));
    expect(() => service.setAiClassifications(project.id, [
      { imageId: first.id, classification: "front", confidence: 0.8 },
      { imageId: first.id, classification: "front", confidence: 0.8 },
    ])).toThrowError(new ServiceError("CLASSIFICATION_SET_INVALID", 400, "Classifications must include every project image exactly once"));

    service.setAiClassifications(project.id, [
      { imageId: first.id, classification: "back", confidence: 0.9 },
      { imageId: second.id, classification: "front", confidence: 0.9 },
    ]);
    expect(service.getReferenceImage(first.id)).toMatchObject({ aiClassification: "back", userClassification: "detail", effectiveClassification: "detail", status: "reviewed" });
  });

  it("classifies only upload-backed images and leaves legacy references untouched", () => {
    const project = service.createProject({ name: "Legacy split" });
    const [legacy] = service.addLegacyReferenceImages(project.id, [{ identifier: "sha256:legacy", filename: "legacy.png" }]);
    const [uploaded] = service.addReferenceImages(project.id, [{ storagePath: `${project.id}/one.png`, originalFilename: "one.png", mimeType: "image/png", size: 8 }]);
    service.setAiClassifications(project.id, [{ imageId: uploaded.id, classification: "front", confidence: 0.9 }]);
    expect(service.getReferenceImage(legacy.id)).toMatchObject({ uploadBacked: false, storagePath: null, aiClassification: null });
    expect(service.getReferenceImage(uploaded.id)).toMatchObject({ uploadBacked: true, aiClassification: "front" });
  });

  it("rejects AI candidate evidence from a legacy image the provider could not read", () => {
    const project = service.createProject({ name: "Legacy evidence" });
    service.upsertProductProfile(project.id, { facts });
    const [legacy] = service.addLegacyReferenceImages(project.id, [{ identifier: "sha256:legacy", filename: "legacy.png" }]);
    expect(() => service.replaceAiCandidates(project.id, [{ field: "material", value: "linen", confidence: 0.8, evidenceImageIds: [legacy.id] }]))
      .toThrowError(new ServiceError("INVALID_EVIDENCE_IMAGE", 400, "Candidate evidence must reference an uploaded image in the project"));
  });

  it("does not copy source-project upload paths or AI evidence when duplicating a project", () => {
    const source = service.createProject({ name: "Source uploads" });
    service.upsertProductProfile(source.id, { facts });
    const [image] = service.addReferenceImages(source.id, [{ storagePath: `${source.id}/front.png`, originalFilename: "front.png", mimeType: "image/png", size: 8 }]);
    service.replaceAiCandidates(source.id, [{ field: "material", value: "linen", confidence: 0.8, evidenceImageIds: [image.id] }]);
    const duplicate = service.duplicateProject(source.id);
    expect(service.getProductProfile(duplicate.id).referenceImages).toEqual([]);
    expect(service.getProductProfile(duplicate.id).aiCandidates).toEqual([]);
  });

  it("does not erase uploaded images or pending AI candidates when confirmed facts are edited", () => {
    const project = service.createProject({ name: "Edit facts" });
    service.upsertProductProfile(project.id, { facts });
    const [image] = service.addReferenceImages(project.id, [{ storagePath: `${project.id}/front.png`, originalFilename: "front.png", mimeType: "image/png", size: 8 }]);
    service.replaceAiCandidates(project.id, [{ field: "material", value: "linen", confidence: 0.8, evidenceImageIds: [image.id] }]);
    service.upsertProductProfile(project.id, { facts: { ...facts, title: "Edited title" } });
    expect(service.getProductProfile(project.id).referenceImages.map(({ id }) => id)).toEqual([image.id]);
    expect(service.getProductProfile(project.id).aiCandidates).toHaveLength(1);
  });

  it("stores non-secret AI settings and reports only environment-key configuration", () => {
    service.updateAiSettings({ baseUrl: "https://ai.example/v1", visionModel: "vision", textModel: "text" });
    expect(service.getAiSettings(true)).toEqual({ baseUrl: "https://ai.example/v1", visionModel: "vision", textModel: "text", configured: true });
    expect(JSON.stringify(service.getAiSettings(true))).not.toContain("key");
  });

  it("refreshes persisted builtin definitions when their export contract version changes", () => {
    const staleDefinition = {
      id: "output_amazon_listing",
      label: "Stale output",
      category: "output",
      content: "Stale output content.",
      capabilities: [],
    };
    const editor = new BetterSqlite3(databasePath);
    editor.prepare("UPDATE prompt_modules SET definition_json = ?, version = 1 WHERE id = ?")
      .run(JSON.stringify(staleDefinition), staleDefinition.id);
    editor.close();
    service.close();
    service = createPromptBuilderService(databasePath);

    const refreshed = service.listModules().find((promptModule) => promptModule.id === staleDefinition.id);
    expect(refreshed?.version).toBe(2);
    expect(refreshed?.definition.exportableParameterKeys).toEqual(["width", "metadata"]);
    expect(refreshed?.definition.label).toBe("Amazon listing specification");
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
    service.upsertProductProfile(project.id, { facts });
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
    service.upsertProductProfile(project.id, { facts });
    const design = service.createDesign(project.id, {
      name: "Restore design",
      selectedModuleIds: ["purpose_amazon_listing"],
      parameters: { width: 1000 },
      custom: {},
    });
    const original = service.saveVersion(design.id);
    service.autosaveDesign(design.id, { parameters: { width: 2000 } }, design.revision);

    const restored = service.restoreVersion(design.id, original.id);

    expect(restored.id).not.toBe(original.id);
    expect(restored.versionNumber).toBe(2);
    expect(restored.designSnapshot.parameters).toEqual({ width: 1000 });
    expect(service.listVersions(design.id).map((version) => version.id)).toEqual([
      restored.id,
      original.id,
    ]);
  });

  it("rejects restoring a legacy version that contains hard errors", () => {
    const project = service.createProject({ name: "Legacy hard error" });
    service.upsertProductProfile(project.id, { facts });
    const design = service.createDesign(project.id, { name: "Legacy", selectedModuleIds: ["purpose_amazon_listing"] });
    const original = service.saveVersion(design.id);
    const injector = new BetterSqlite3(databasePath);
    injector.prepare("UPDATE prompt_versions SET compile_snapshot_json = ? WHERE id = ?").run(JSON.stringify({
      ...original.compileSnapshot,
      errors: [{ code: "LEGACY_HARD_ERROR", message: "invalid legacy snapshot", moduleIds: [] }],
    }), original.id);
    injector.close();

    expect(() => service.restoreVersion(design.id, original.id)).toThrowError(expect.objectContaining({ code: "HARD_CONSTRAINTS" }));
    expect(service.listVersions(design.id)).toHaveLength(1);
  });

  it("rejects a polish saved against a stale design revision", () => {
    const project = service.createProject({ name: "Concurrent polish" });
    service.upsertProductProfile(project.id, { facts });
    const design = service.createDesign(project.id, { name: "Polish", selectedModuleIds: ["purpose_amazon_listing"] });
    service.autosaveDesign(design.id, { parameters: { width: 1600 } }, design.revision);

    expect(() => service.savePolishedVersion(design.id, "polished", design.revision, service.compile(design.id).prompt))
      .toThrowError(expect.objectContaining({ code: "STALE_REVISION" }));
    expect(service.listVersions(design.id)).toHaveLength(0);
  });

  it("updates a profile without inserting, replacing, or deleting legacy reference rows", () => {
    const project = service.createProject({ name: "Profile rollback" });
    service.upsertProductProfile(project.id, {
      facts,
    });
    service.addLegacyReferenceImages(project.id, [{ identifier: "sha256:old", filename: "old.png" }]);
    const injector = new BetterSqlite3(databasePath);
    injector.exec(`CREATE TRIGGER fail_reference_insert
      BEFORE INSERT ON reference_images BEGIN SELECT RAISE(ABORT, 'injected failure'); END`);
    injector.close();

    service.upsertProductProfile(project.id, {
      facts: { ...facts, title: "Updated without touching images" },
    });

    expect(service.getProductProfile(project.id)).toMatchObject({
      facts: { title: "Updated without touching images" },
      referenceImages: [{ identifier: "sha256:old", filename: "old.png" }],
    });
  });

  it("rolls back a duplicated project when copying child rows fails", () => {
    const source = service.createProject({ name: "Duplicate rollback" });
    service.upsertProductProfile(source.id, {
      facts,
    });
    service.addLegacyReferenceImages(source.id, [{ identifier: "sha256:source", filename: "source.png" }]);
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
    service.upsertProductProfile(project.id, { facts });
    const design = service.createDesign(project.id, {
      name: "Restore rollback design",
      selectedModuleIds: ["purpose_amazon_listing"],
      parameters: { width: 1000 },
      custom: {},
    });
    const original = service.saveVersion(design.id);
    service.autosaveDesign(design.id, { parameters: { width: 2000 } }, design.revision);
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
    service.upsertProductProfile(project.id, { facts });
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
    service.upsertProductProfile(project.id, { facts });
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

  it("waits for a real competing write lock before allocating cross-connection versions", async () => {
    const project = service.createProject({ name: "Locked versions" });
    service.upsertProductProfile(project.id, { facts });
    const design = service.createDesign(project.id, {
      name: "Locked design",
      selectedModuleIds: ["purpose_amazon_listing"],
      parameters: {},
      custom: {},
    });
    const modulePath = createRequire(import.meta.url).resolve("better-sqlite3");
    const locker = new Worker(`
      const { parentPort, workerData } = require("node:worker_threads");
      const Database = require(workerData.modulePath);
      const db = new Database(workerData.databasePath);
      db.exec("BEGIN IMMEDIATE");
      parentPort.postMessage("locked");
      setTimeout(() => {
        db.exec("COMMIT");
        db.close();
        parentPort.postMessage("released");
      }, 250);
    `, { eval: true, workerData: { databasePath, modulePath } });
    await new Promise<void>((resolve, reject) => {
      locker.once("message", () => resolve());
      locker.once("error", reject);
    });
    const startedAt = Date.now();

    const first = service.saveVersion(design.id);
    const waitedMs = Date.now() - startedAt;
    const secondService = createPromptBuilderService(databasePath);
    try {
      const second = secondService.saveVersion(design.id);
      expect(waitedMs).toBeGreaterThanOrEqual(150);
      expect([first.versionNumber, second.versionNumber]).toEqual([1, 2]);
    } finally {
      secondService.close();
      await locker.terminate();
    }
  });

  it("exports a stable, safe versioned JSON document", () => {
    const project = service.createProject({ name: "Export" });
    service.upsertProductProfile(project.id, {
      facts,
    });
    service.addLegacyReferenceImages(project.id, [{ identifier: "sha256:front", filename: "front.png" }]);
    const design = service.createDesign(project.id, {
      name: "Export design",
      selectedModuleIds: ["purpose_amazon_listing", "output_amazon_listing"],
      parameters: {
        width: 1024,
        rogueSetting: "must-not-export",
        authHeader: "auth-header-value",
        bearerCredential: "bearer-credential-value",
        sessionId: "session-id-value",
        cookieJar: "cookie-jar-value",
        metadata: {
          tokenCount: 37,
          authHeader: "nested-auth-header",
          entries: [{ bearerCredential: "nested-bearer", label: "safe entry" }],
        },
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
        purpose: "Summer",
        lighting: "Soft daylight",
        headline: "must be removed",
        output: 42,
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
        metadata: {
          tokenCount: 37,
          entries: [{ label: "safe entry" }],
        },
      },
      custom: {
        purpose: "Summer",
        lighting: "Soft daylight",
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
    expect(json).not.toContain("rogueSetting");
    expect(json).not.toContain("auth-header-value");
    expect(json).not.toContain("bearer-credential-value");
    expect(json).not.toContain("session-id-value");
    expect(json).not.toContain("cookie-jar-value");
    expect(json).not.toContain("nested-auth-header");
    expect(json).not.toContain("nested-bearer");
    expect(json).not.toContain("must be removed");
    expect(json).toContain("tokenCount");
    expect(json).not.toContain("imageData");
  });
});
