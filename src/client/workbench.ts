export interface CompileLike {
  sections: Array<{ title: string; content: string }>;
  errors: Array<{ code: string; message: string; moduleIds: string[] }>;
  warnings: Array<{ code: string; message: string; moduleIds: string[] }>;
}

export interface SectionDiff {
  title: string;
  before: string;
  after: string;
  status: "added" | "removed" | "changed";
}

export function diffVersions(before: Pick<CompileLike, "sections">, after: Pick<CompileLike, "sections">): SectionDiff[] {
  const left = new Map(before.sections.map((section) => [section.title, section.content]));
  const right = new Map(after.sections.map((section) => [section.title, section.content]));
  const titles = [...new Set([...left.keys(), ...right.keys()])];
  return titles.flatMap((title) => {
    const previous = left.get(title) ?? "";
    const next = right.get(title) ?? "";
    if (previous === next) return [];
    return [{ title, before: previous, after: next, status: !left.has(title) ? "added" as const : !right.has(title) ? "removed" as const : "changed" as const }];
  });
}

const sectionNames: Record<string, string> = {
  "Product Facts (Immutable)": "产品事实（已锁定）",
  "Purpose & Subject": "用途与主体",
  "Composition & Camera": "构图与镜头",
  Scene: "场景",
  "Style, Color & Lighting": "风格、色彩与灯光",
  Text: "文字",
  "Output Specification": "输出规格",
  "Negative Constraints": "负向约束",
};
const issueNames: Record<string, string> = {
  BACK_VIEW_EVIDENCE_REQUIRED: "缺少背面图证据",
  UNKNOWN_MODULE_ID: "存在未知模块",
  WHITE_BACKGROUND_WITH_SCENE: "纯白背景与环境场景冲突",
  NO_TEXT_WITH_TITLE: "无文字与标题设置冲突",
};

export function describeCompile(compile: CompileLike): string {
  const sections = compile.sections.map(({ title, content }) => `${sectionNames[title] ?? title}：${content.replace(/^[-\s]+/, "")}`);
  const errors = compile.errors.map((item) => `必须修正：${issueNames[item.code] ?? item.message}`);
  const warnings = compile.warnings.map((item) => `建议检查：${issueNames[item.code] ?? item.message}`);
  return [...sections, ...errors, ...warnings].join("\n\n");
}

export function createDebouncer<T>(callback: (value: T) => void, delay: number) {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let pending: T | undefined;
  return {
    schedule(value: T) {
      if (timer) clearTimeout(timer);
      pending = value;
      timer = setTimeout(() => { timer = undefined; const value = pending; pending = undefined; if (value !== undefined) callback(value); }, delay);
    },
    cancel() { if (timer) clearTimeout(timer); timer = undefined; pending = undefined; },
    flush() { if (timer) clearTimeout(timer); timer = undefined; const value = pending; pending = undefined; if (value !== undefined) callback(value); },
  };
}

export const imageClassificationOptions = ["main", "front", "back", "side", "detail", "variant", "unknown"] as const;

export function canExportCompile(compile: { errors: unknown[]; warnings: unknown[] } | null): boolean {
  return compile !== null && compile.errors.length === 0;
}

function sensitiveKey(key: string): boolean {
  const normalized = key.toLowerCase().replace(/[^a-z0-9]/g, "");
  return normalized.includes("authorization") || normalized.includes("authheader")
    || normalized.includes("apikey") || normalized.includes("privatekey")
    || normalized.includes("bearercredential") || normalized.includes("cookiejar")
    || normalized.includes("password") || normalized.includes("secret")
    || normalized.includes("sessionid") || normalized.endsWith("token") || normalized.endsWith("cookie");
}

export function shouldAcceptCompile(responseRevision: number, currentRevision: number): boolean {
  return responseRevision === currentRevision;
}

export function isCompileContextCurrent(
  envelope: { designId: string; revision: number },
  context: { designId: string; expectedRevision: number; localRevision: number; responseLocalRevision: number },
): boolean {
  return envelope.designId === context.designId
    && envelope.revision === context.expectedRevision + 1
    && context.responseLocalRevision === context.localRevision;
}

export function isNavigationRequestCurrent(request: number, currentRequest: number, requestedDesignId: string, activeDesignId: string | null): boolean {
  return request === currentRequest && requestedDesignId === activeDesignId;
}

export type OperationToken = { generation: number; designId: string | null };

export function createOperationController(initialDesignId: string | null = null) {
  let generation = 0;
  let activeDesignId = initialDesignId;
  let mounted = true;
  return {
    capture(designId: string | null = activeDesignId): OperationToken {
      return { generation, designId };
    },
    activate(designId: string | null) {
      generation += 1;
      activeDesignId = designId;
    },
    invalidate() {
      generation += 1;
    },
    isCurrent(token: OperationToken): boolean {
      return mounted && token.generation === generation && token.designId === activeDesignId;
    },
    mount(designId: string | null = activeDesignId) {
      mounted = true;
      generation += 1;
      activeDesignId = designId;
    },
    dispose() {
      mounted = false;
      generation += 1;
      activeDesignId = null;
    },
    get generation() { return generation; },
    get activeDesignId() { return activeDesignId; },
  };
}

export type OperationController = ReturnType<typeof createOperationController>;

export function createSerialExecutor() {
  let tail: Promise<unknown> = Promise.resolve();
  return function run<T>(operation: () => Promise<T>): Promise<T> {
    const result = tail.then(operation, operation);
    tail = result.then(() => undefined, () => undefined);
    return result;
  };
}

type ParameterModule = {
  id: string;
  definition: { exportableParameterKeys?: readonly string[] };
};

export function declaredParameterKeys(selectedModuleIds: readonly string[], modules: readonly ParameterModule[]): string[] {
  const selected = new Set(selectedModuleIds);
  return [...new Set(modules
    .filter((module) => selected.has(module.id))
    .flatMap((module) => module.definition.exportableParameterKeys ?? []))];
}

export function reconcileDeclaredParameters(parameters: Record<string, unknown>, allowedKeys: readonly string[]): Record<string, unknown> {
  const allowed = new Set(allowedKeys);
  return Object.fromEntries(Object.entries(parameters).filter(([key]) => allowed.has(key)));
}

export function parseParameterValue(raw: string): unknown {
  const trimmed = raw.trim();
  if (!trimmed) return "";
  try { return JSON.parse(trimmed); } catch { return raw; }
}

export function formatParameterValue(value: unknown): string {
  if (value === undefined) return "";
  return typeof value === "string" ? value : JSON.stringify(value);
}

export function parseExportableParameterKeys(raw: string): { keys: string[]; error: string } {
  const keys = raw.split(/[,，\s]+/).map((key) => key.trim()).filter(Boolean);
  if (keys.length > 50) return { keys, error: "导出参数键最多可配置 50 个" };
  const invalid = keys.find((key) => !/^[a-z][a-zA-Z0-9_]{0,63}$/.test(key));
  if (invalid) return { keys, error: `“${invalid}” 不是合法的 ASCII 参数键（仅限字母、数字和下划线）` };
  const duplicate = keys.find((key, index) => keys.indexOf(key) !== index);
  if (duplicate) return { keys, error: `参数键“${duplicate}”重复` };
  return { keys, error: "" };
}

function cleanValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(cleanValue);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).filter(([key]) => !sensitiveKey(key)).map(([key, nested]) => [key, cleanValue(nested)]));
  return value;
}

export function safeDraftExport(input: {
  selectedModuleIds: string[];
  parameters: Record<string, unknown>;
  custom: Record<string, unknown>;
  allowedParameterKeys: string[];
  compile: unknown;
}) {
  const allowed = new Set(input.allowedParameterKeys);
  const categories = new Set<string>(moduleCategories);
  return {
    formatVersion: 1,
    selectedModuleIds: [...input.selectedModuleIds],
    parameters: Object.fromEntries(Object.entries(input.parameters).filter(([key]) => allowed.has(key) && !sensitiveKey(key)).map(([key, value]) => [key, cleanValue(value)])),
    custom: Object.fromEntries(Object.entries(input.custom).filter(([key, value]) => categories.has(key) && typeof value === "string")),
    compile: input.compile,
  };
}
import { moduleCategories } from "@/domain/types";
