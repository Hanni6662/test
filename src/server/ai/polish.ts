import { SECTION_TITLES } from "@/domain/prompt-compiler";
import type { CompileResult } from "@/domain/types";
import { ServiceError } from "@/server/persistence/service";

const SAFE_INSERTION_ALLOWLIST = new Set(["a", "an", "that", "the", "which"]);
const PROTECTED_SECTION_INDEXES = [0, 6, 7] as const;

function fail(): never {
  throw new ServiceError("AI_POLISH_UNSAFE", 422, "Polished prompt must preserve the fixed structure, facts, vocabulary, and hard constraints");
}

function normalizeWhitespace(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function parseFixedSections(prompt: string): string[] {
  const lines = prompt.split(/\r?\n/);
  const sections: string[] = [];
  let current = -1;
  for (const line of lines) {
    if (line.trimStart().startsWith("## ")) {
      current += 1;
      if (current >= SECTION_TITLES.length || line.trim() !== `## ${SECTION_TITLES[current]}`) fail();
      sections.push("");
    } else {
      if (current < 0 && line.trim()) fail();
      if (current >= 0) sections[current] += `${line}\n`;
    }
  }
  if (sections.length !== SECTION_TITLES.length) fail();
  return sections.map((section) => section.trim());
}

function tokens(value: string): string[] {
  return [...value.toLowerCase().matchAll(/[\p{L}\p{N}]+(?:['’-][\p{L}\p{N}]+)*/gu)].map(([token]) => token);
}

export function validatePolishedPrompt(original: string | CompileResult, polished: string): string {
  const source = typeof original === "string" ? original : original.prompt;
  const sourceSections = parseFixedSections(source);
  const polishedSections = parseFixedSections(polished);
  for (const index of PROTECTED_SECTION_INDEXES) {
    if (normalizeWhitespace(sourceSections[index]) !== normalizeWhitespace(polishedSections[index])) fail();
  }

  const sourceVocabulary = new Set(tokens(source));
  const polishedVocabulary = new Set(tokens(polished));
  if ([...polishedVocabulary].some((token) => !sourceVocabulary.has(token) && !SAFE_INSERTION_ALLOWLIST.has(token))) fail();

  if ([...sourceVocabulary].some((token) => !polishedVocabulary.has(token))) fail();
  for (let index = 1; index <= 5; index += 1) {
    const sourceContent = tokens(sourceSections[index]);
    const polishedContent = tokens(polishedSections[index]);
    let sourcePosition = 0;
    for (const token of polishedContent) {
      if (token === sourceContent[sourcePosition]) sourcePosition += 1;
      else if (!SAFE_INSERTION_ALLOWLIST.has(token)) fail();
    }
    if (sourcePosition !== sourceContent.length) fail();
  }
  return polished;
}
