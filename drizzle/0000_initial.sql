PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY NOT NULL,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reference_images (
  id TEXT PRIMARY KEY NOT NULL,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  identifier TEXT NOT NULL,
  filename TEXT NOT NULL,
  storage_path TEXT,
  mime_type TEXT,
  size INTEGER,
  ai_classification TEXT,
  user_classification TEXT,
  confidence INTEGER,
  status TEXT NOT NULL DEFAULT 'uploaded',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_profiles (
  project_id TEXT PRIMARY KEY NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  facts_json TEXT NOT NULL,
  ai_candidates_json TEXT NOT NULL DEFAULT '[]',
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_settings (
  id INTEGER PRIMARY KEY NOT NULL CHECK(id = 1),
  base_url TEXT NOT NULL,
  vision_model TEXT NOT NULL,
  text_model TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prompt_modules (
  id TEXT PRIMARY KEY NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('builtin', 'custom')),
  definition_json TEXT NOT NULL,
  version INTEGER NOT NULL,
  enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS image_designs (
  id TEXT PRIMARY KEY NOT NULL,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  selected_module_ids_json TEXT NOT NULL,
  parameters_json TEXT NOT NULL,
  custom_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prompt_versions (
  id TEXT PRIMARY KEY NOT NULL,
  design_id TEXT NOT NULL REFERENCES image_designs(id) ON DELETE CASCADE,
  version_number INTEGER NOT NULL,
  product_snapshot_json TEXT NOT NULL,
  module_snapshot_json TEXT NOT NULL,
  design_snapshot_json TEXT NOT NULL,
  compile_snapshot_json TEXT NOT NULL,
  compiler_version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(design_id, version_number)
);
CREATE INDEX IF NOT EXISTS reference_images_project_idx ON reference_images(project_id);
CREATE INDEX IF NOT EXISTS image_designs_project_idx ON image_designs(project_id);
CREATE INDEX IF NOT EXISTS prompt_versions_design_idx ON prompt_versions(design_id);
