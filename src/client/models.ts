import type { CompileResult, ModuleCategory, ProductFacts, PromptModuleDefinition } from "@/domain/types";

export interface Project { id: string; name: string; createdAt: string; updatedAt: string }
export interface Design { id: string; projectId: string; name: string; selectedModuleIds: string[]; parameters: Record<string, unknown>; custom: Record<string, unknown>; polishedPrompt: string | null; polishedBasePromptHash: string | null; revision: number }
export interface CompileEnvelope { designId: string; revision: number; design: Design; compile: CompileResult }
export interface CompileState extends CompileEnvelope { localRevision: number }
export interface StoredModule { id: string; kind: "builtin" | "custom"; version: number; enabled: boolean; definition: PromptModuleDefinition }
export interface ReferenceImage { id: string; filename: string; uploadBacked: boolean; aiClassification: string | null; userClassification: string | null; effectiveClassification: string; confidence: number | null }
export interface Candidate { id: string; field: keyof ProductFacts; value: unknown; confidence: number; evidenceImageIds: string[] }
export interface ProductProfile { facts: ProductFacts; referenceImages: ReferenceImage[]; aiCandidates?: Candidate[] }
export interface Version { id: string; designId: string; versionNumber: number; designSnapshot: Design; compileSnapshot: CompileResult; polishedPrompt: string | null; polishedBasePromptHash: string | null; polishedPromptValid: boolean; createdAt: string; compilerVersion: string }
export interface AiSettings { baseUrl: string; visionModel: string; textModel: string; configured: boolean }
export type View = "projects" | "modules" | "settings" | "profile" | "builder" | "history";
export const categoryNames: Record<ModuleCategory, string> = { purpose: "用途", subject: "主体", composition: "构图", camera: "镜头", scene: "场景", style: "风格", color: "色彩", lighting: "灯光", text: "文字", output: "输出", negative: "负向约束" };
export const emptyFacts: ProductFacts = { title: "", category: "", audience: "", material: "", colors: [], features: [], backViewEvidence: false };
