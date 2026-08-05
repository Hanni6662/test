import type { CompileResult } from "@/domain/types";
import { ServiceError } from "@/server/persistence/service";

function protectedLines(prompt: string): string[] {
  const lines = prompt.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const factsStart = lines.indexOf("## Product Facts (Immutable)");
  const factsEnd = lines.findIndex((line, index) => index > factsStart && line.startsWith("## "));
  const factLines = factsStart >= 0 ? lines.slice(factsStart, factsEnd < 0 ? lines.length : factsEnd) : [];
  const hardSections = ["## Negative Constraints", "## Output Specification"];
  const constraintLines = lines.filter((line, index) => hardSections.some((heading) => {
    const start = lines.indexOf(heading);
    if (start < 0 || index < start) return false;
    const next = lines.findIndex((candidate, candidateIndex) => candidateIndex > start && candidate.startsWith("## "));
    return next < 0 || index < next;
  }));
  return [...new Set([...factLines, ...constraintLines])];
}

export function validatePolishedPrompt(original: string | CompileResult, polished: string): string {
  const source = typeof original === "string" ? original : original.prompt;
  const missing = protectedLines(source).filter((line) => !polished.includes(line));
  const factKeys = ["Title", "Category", "Audience", "Material", "Colors", "Verified features", "Back-view evidence"];
  const sourceFacts = new Map(source.split(/\r?\n/).filter((line) => factKeys.some((key) => line.startsWith(`${key}:`))).map((line) => [line.slice(0, line.indexOf(":")), line]));
  const contradictoryFact = polished.split(/\r?\n/).some((line) => {
    const separator = line.indexOf(":"); if (separator < 0) return false;
    const expected = sourceFacts.get(line.slice(0, separator)); return expected !== undefined && line !== expected;
  });
  const overrideInstruction = /\b(ignore|override|disregard|contradict|replace|change)\b.{0,80}\b(facts?|constraints?|negative|immutable|requirements?)\b/i.test(polished);
  if (missing.length || contradictoryFact || overrideInstruction) throw new ServiceError("AI_POLISH_UNSAFE", 422, "Polished prompt must preserve product facts and hard constraints without contradictions");
  return polished;
}
