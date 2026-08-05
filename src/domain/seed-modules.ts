import type { PromptModuleDefinition } from "./types";

// A deliberately small, hand-curated catalog distilled from the reference notes.
// The source document is not parsed or loaded at runtime.
export const seedPromptModules: readonly PromptModuleDefinition[] = [
  {
    id: "purpose_amazon_aplus",
    label: "Amazon A+ content",
    category: "purpose",
    content: "Design a premium Amazon A+ commerce image that explains the garment's value at a glance.",
  },
  {
    id: "purpose_amazon_listing",
    label: "Amazon listing",
    category: "purpose",
    content: "Create a conversion-focused Amazon listing image with immediate product clarity.",
  },
  {
    id: "purpose_temu",
    label: "Temu marketplace",
    category: "purpose",
    content: "Create an energetic Temu marketplace visual with a clear, value-led product presentation.",
  },
  {
    id: "subject_real_child_model",
    label: "Real child model",
    category: "subject",
    content: "Show the children's garment naturally worn by an age-appropriate real model with an authentic fit.",
  },
  {
    id: "subject_flat_lay",
    label: "Flat lay",
    category: "subject",
    content: "Present the complete garment as a neat flat lay, preserving its true silhouette and details.",
  },
  {
    id: "composition_centered_hero",
    label: "Centered hero",
    category: "composition",
    content: "Use a centered hero composition with generous breathing room and an instantly readable silhouette.",
  },
  {
    id: "composition_aplus_split",
    label: "A+ split layout",
    category: "composition",
    content: "Use a balanced product-and-detail split composition suitable for an Amazon A+ module.",
  },
  {
    id: "camera_front_view",
    label: "Front view",
    category: "camera",
    content: "Use a straight-on front view at garment height without perspective distortion.",
  },
  {
    id: "camera_back_view",
    label: "Back view",
    category: "camera",
    content: "Show a faithful straight-on back view based only on supplied back-view evidence.",
  },
  {
    id: "scene_white_background",
    label: "Pure white background",
    category: "scene",
    content: "Place the product on a seamless pure white background (RGB 255, 255, 255).",
  },
  {
    id: "scene_garden_party",
    label: "Garden party",
    category: "scene",
    content: "Stage a bright summer garden-party setting with soft greenery kept secondary to the garment.",
  },
  {
    id: "scene_cozy_playroom",
    label: "Cozy playroom",
    category: "scene",
    content: "Use a tidy, warm children's playroom that feels believable and does not obscure the clothing.",
  },
  {
    id: "style_clean_commercial",
    label: "Clean commercial",
    category: "style",
    content: "Use polished contemporary children's-fashion advertising with realistic fabric texture.",
  },
  {
    id: "style_playful_editorial",
    label: "Playful editorial",
    category: "style",
    content: "Use a playful editorial mood while keeping the product accurate and commerce-ready.",
  },
  {
    id: "color_pastel_summer",
    label: "Pastel summer palette",
    category: "color",
    content: "Use a soft summer pastel palette that supports, but never shifts, the product's true colors.",
  },
  {
    id: "color_warm_neutral",
    label: "Warm neutral palette",
    category: "color",
    content: "Use warm neutral surroundings with restrained accent colors and accurate white balance.",
  },
  {
    id: "lighting_soft_daylight",
    label: "Soft daylight",
    category: "lighting",
    content: "Use soft diffused daylight with gentle shadows and clear textile detail.",
  },
  {
    id: "lighting_studio_even",
    label: "Even studio light",
    category: "lighting",
    content: "Use even high-key studio lighting without blown highlights or harsh garment shadows.",
  },
  {
    id: "text_title_headline",
    label: "Title headline",
    category: "text",
    content: "Reserve a clean text-safe area and add one short product-title headline with exact spelling.",
  },
  {
    id: "text_no_text",
    label: "No text",
    category: "text",
    content: "Do not render any words, letters, badges, logos, watermarks, or typography.",
  },
  {
    id: "output_amazon_listing",
    label: "Amazon listing specification",
    category: "output",
    content: "Output a square 1:1 high-resolution marketplace image with the product filling about 85% of frame.",
  },
  {
    id: "output_aplus_wide",
    label: "Amazon A+ wide specification",
    category: "output",
    content: "Output a crisp wide-format asset with safe margins for responsive Amazon A+ placement.",
  },
  {
    id: "negative_product_integrity",
    label: "Product integrity",
    category: "negative",
    content: "No invented trims, altered prints, changed colors, malformed anatomy, duplicate garments, blur, or compression artifacts.",
  },
];

