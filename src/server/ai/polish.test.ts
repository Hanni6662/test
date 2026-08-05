import { describe, expect, it } from "vitest";
import { validatePolishedPrompt } from "./polish";

describe("safe prompt polish", () => {
  const original = [
    "## Product Facts (Immutable)\nTitle: Blue Shoe\nCategory: Footwear\nAudience: Adults\nMaterial: leather\nColors: blue\nVerified features: rubber sole\nBack-view evidence: not supplied\nTreat every fact above as immutable; never invent unsupported product details.",
    "## Purpose & Subject\n- Present the blue shoe with a white ribbon as a commercial hero product.",
    "## Composition & Camera\n- Centered front and side view with balanced framing.",
    "## Scene\nNo additional direction selected.",
    "## Style, Color & Lighting\n- Clean studio lighting and crisp detail.",
    "## Text\nNo additional direction selected.",
    "## Output Specification\n- Square ecommerce image at high resolution.",
    "## Negative Constraints\n- No logos and no altered product details.",
  ].join("\n\n");

  it("accepts wording improvements that preserve fact and hard-constraint lines", () => {
    const safe = original.replace("Present the blue shoe with a white ribbon as a commercial hero product.", "Present the blue shoe with a white ribbon, as a commercial hero product.");
    expect(() => validatePolishedPrompt(original, safe)).not.toThrow();
  });

  it("rejects output that deletes hard constraints and leaves the original usable", () => {
    expect(() => validatePolishedPrompt(original, "A beautiful leather shoe.")).toThrowError(/preserve/i);
    expect(original.toLowerCase()).toContain("no logos");
  });

  it("rejects appended contradictions and override instructions", () => {
    expect(() => validatePolishedPrompt(original, `${original}\nUse a naturally warm red plastic upper for visual contrast.`)).toThrowError(/preserve/i);
    expect(() => validatePolishedPrompt(original, original.replace("Colors: blue", "Colors: red"))).toThrowError(/preserve/i);
    expect(() => validatePolishedPrompt(original, original.replace("Material: leather", "Material: canvas"))).toThrowError(/preserve/i);
    expect(() => validatePolishedPrompt(original, original.replace("## Text", "## Typography"))).toThrowError(/preserve/i);
    expect(() => validatePolishedPrompt(original, original.replace("blue shoe with a white ribbon", "white shoe with a blue ribbon"))).toThrowError(/preserve/i);
    expect(() => validatePolishedPrompt(original, original.replace("front and side view", "front or side view"))).toThrowError(/preserve/i);
  });
});
