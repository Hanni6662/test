import type { PromptModuleDefinition } from "./types";

// A deliberately small, hand-curated catalog distilled from the reference notes.
// The source document is not parsed or loaded at runtime.
const curatedPromptModules: PromptModuleDefinition[] = [
  {
    id: "purpose_amazon_aplus",
    label: "Amazon A+ content",
    category: "purpose",
    content: "Design a premium Amazon A+ commerce image that explains the garment's value at a glance.",
    contentZh: "设计高品质的亚马逊 A+ 商业图片，让顾客一眼理解服装价值。",
  },
  {
    id: "purpose_amazon_listing",
    label: "Amazon listing",
    category: "purpose",
    content: "Create a conversion-focused Amazon listing image with immediate product clarity.",
    contentZh: "制作以转化为目标的亚马逊商品图片，清晰直观地呈现产品。",
  },
  {
    id: "purpose_temu",
    label: "Temu marketplace",
    category: "purpose",
    content: "Create an energetic Temu marketplace visual with a clear, value-led product presentation.",
    contentZh: "制作富有活力的 Temu 商城视觉，以清晰且突出价值的方式呈现产品。",
  },
  {
    id: "subject_real_child_model",
    label: "Real child model",
    category: "subject",
    content: "Show the children's garment naturally worn by an age-appropriate real model with an authentic fit.",
    contentZh: "由年龄合适的真人儿童模特自然穿着服装，真实呈现合身效果。",
  },
  {
    id: "subject_flat_lay",
    label: "Flat lay",
    category: "subject",
    content: "Present the complete garment as a neat flat lay, preserving its true silhouette and details.",
    contentZh: "整洁平铺展示完整服装，保留真实轮廓与细节。",
  },
  {
    id: "composition_centered_hero",
    label: "Centered hero",
    category: "composition",
    content: "Use a centered hero composition with generous breathing room and an instantly readable silhouette.",
    contentZh: "采用居中的主视觉构图，保留充足留白，让服装轮廓一目了然。",
  },
  {
    id: "composition_aplus_split",
    label: "A+ split layout",
    category: "composition",
    content: "Use a balanced product-and-detail split composition suitable for an Amazon A+ module.",
    contentZh: "采用产品与细节均衡分区的构图，适用于亚马逊 A+ 模块。",
  },
  {
    id: "camera_front_view",
    label: "Front view",
    category: "camera",
    content: "Use a straight-on front view at garment height without perspective distortion.",
    contentZh: "以服装高度正面平视拍摄，避免透视变形。",
  },
  {
    id: "camera_back_view",
    label: "Back view",
    category: "camera",
    content: "Show a faithful straight-on back view based only on supplied back-view evidence.",
    contentZh: "仅依据已提供的背面证据，忠实展示正后方视角。",
    capabilities: ["view:back"],
  },
  {
    id: "scene_white_background",
    label: "Pure white background",
    category: "scene",
    content: "Place the product on a seamless pure white background (RGB 255, 255, 255).",
    contentZh: "将产品置于无缝纯白背景上（RGB 255, 255, 255）。",
    capabilities: ["background:white"],
  },
  {
    id: "scene_garden_party",
    label: "Garden party",
    category: "scene",
    content: "Stage a bright summer garden-party setting with soft greenery kept secondary to the garment.",
    contentZh: "营造明亮的夏日花园派对场景，以柔和绿植衬托服装且不喧宾夺主。",
    capabilities: ["scene:environment"],
  },
  {
    id: "scene_cozy_playroom",
    label: "Cozy playroom",
    category: "scene",
    content: "Use a tidy, warm children's playroom that feels believable and does not obscure the clothing.",
    contentZh: "使用整洁温暖且真实可信的儿童游戏室场景，不遮挡服装。",
    capabilities: ["scene:environment"],
  },
  {
    id: "style_clean_commercial",
    label: "Clean commercial",
    category: "style",
    content: "Use polished contemporary children's-fashion advertising with realistic fabric texture.",
    contentZh: "采用精致现代的童装商业广告风格，真实呈现面料质感。",
  },
  {
    id: "style_playful_editorial",
    label: "Playful editorial",
    category: "style",
    content: "Use a playful editorial mood while keeping the product accurate and commerce-ready.",
    contentZh: "采用活泼的编辑大片氛围，同时保持产品准确并适合商业展示。",
  },
  {
    id: "color_pastel_summer",
    label: "Pastel summer palette",
    category: "color",
    content: "Use a soft summer pastel palette that supports, but never shifts, the product's true colors.",
    contentZh: "使用柔和的夏日粉彩配色衬托产品，但绝不改变产品真实颜色。",
  },
  {
    id: "color_warm_neutral",
    label: "Warm neutral palette",
    category: "color",
    content: "Use warm neutral surroundings with restrained accent colors and accurate white balance.",
    contentZh: "使用暖中性色环境与克制的点缀色，并保持准确白平衡。",
  },
  {
    id: "lighting_soft_daylight",
    label: "Soft daylight",
    category: "lighting",
    content: "Use soft diffused daylight with gentle shadows and clear textile detail.",
    contentZh: "使用柔和漫射日光，呈现轻柔阴影与清晰织物细节。",
  },
  {
    id: "lighting_studio_even",
    label: "Even studio light",
    category: "lighting",
    content: "Use even high-key studio lighting without blown highlights or harsh garment shadows.",
    contentZh: "使用均匀的高调棚拍光，避免高光过曝或服装出现生硬阴影。",
  },
  {
    id: "text_title_headline",
    label: "Title headline",
    category: "text",
    content: "Reserve a clean text-safe area and add one short product-title headline with exact spelling.",
    contentZh: "预留干净的文字安全区，并添加一条拼写准确的简短产品标题。",
    capabilities: ["text:title"],
  },
  {
    id: "text_no_text",
    label: "No text",
    category: "text",
    content: "Do not render any words, letters, badges, logos, watermarks, or typography.",
    contentZh: "不得生成任何单词、字母、徽章、标志、水印或排版文字。",
    capabilities: ["text:none"],
  },
  {
    id: "output_amazon_listing",
    label: "Amazon listing specification",
    category: "output",
    content: "Output a square 1:1 high-resolution marketplace image with the product filling about 85% of frame.",
    contentZh: "输出 1:1 正方形高分辨率商城图片，产品约占画面的 85%。",
    exportableParameterKeys: ["width", "metadata"],
  },
  {
    id: "output_aplus_wide",
    label: "Amazon A+ wide specification",
    category: "output",
    content: "Output a crisp wide-format asset with safe margins for responsive Amazon A+ placement.",
    contentZh: "输出清晰的宽幅素材，并保留适合亚马逊 A+ 响应式布局的安全边距。",
  },
  {
    id: "negative_product_integrity",
    label: "Product integrity",
    category: "negative",
    content: "No invented trims, altered prints, changed colors, malformed anatomy, duplicate garments, blur, or compression artifacts.",
    contentZh: "禁止虚构装饰、篡改印花或颜色、异常人体结构、重复服装、模糊及压缩瑕疵。",
  },
];

export const seedPromptModules: readonly PromptModuleDefinition[] = Object.freeze(
  curatedPromptModules.map((module) =>
    Object.freeze({
      ...module,
      ...(module.capabilities
        ? { capabilities: Object.freeze([...module.capabilities]) }
        : {}),
      ...(module.exportableParameterKeys
        ? { exportableParameterKeys: Object.freeze([...module.exportableParameterKeys]) }
        : {}),
    }),
  ),
);
