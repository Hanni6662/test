import { randomUUID } from "node:crypto";
import { mkdir, rmdir, unlink, writeFile as fsWriteFile } from "node:fs/promises";
import { basename, extname, isAbsolute, relative, resolve, sep } from "node:path";

export const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
export const MAX_BATCH_IMAGES = 20;

const TYPES = {
  "image/jpeg": { extension: ".jpg", matches: (b: Uint8Array) => b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff },
  "image/png": { extension: ".png", matches: (b: Uint8Array) => [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a].every((v, i) => b[i] === v) },
  "image/webp": { extension: ".webp", matches: (b: Uint8Array) => String.fromCharCode(...b.slice(0, 4)) === "RIFF" && String.fromCharCode(...b.slice(8, 12)) === "WEBP" },
} as const;

export class UploadError extends Error {
  constructor(public readonly code: string, public readonly status: number, message: string) {
    super(message);
    this.name = "UploadError";
  }
}

export interface StoredUpload {
  storagePath: string;
  originalFilename: string;
  mimeType: keyof typeof TYPES;
  size: number;
}

function projectDirectory(uploadRoot: string, projectId: string): string {
  if (!/^[a-zA-Z0-9_-]{1,100}$/.test(projectId)) throw new UploadError("INVALID_PROJECT_ID", 400, "Invalid project ID");
  return resolve(uploadRoot, projectId);
}

export interface StoreUploadedFilesOptions { writeFile?: typeof fsWriteFile }

export async function storeUploadedFiles(uploadRoot: string, projectId: string, files: File[], options: StoreUploadedFilesOptions = {}): Promise<StoredUpload[]> {
  if (files.length === 0) throw new UploadError("NO_IMAGES", 400, "At least one image is required");
  if (files.length > MAX_BATCH_IMAGES) throw new UploadError("TOO_MANY_IMAGES", 413, "A batch may contain at most 20 images");
  const validated: Array<StoredUpload & { bytes: Uint8Array }> = [];
  for (const file of files) {
    if (file.size > MAX_IMAGE_BYTES) throw new UploadError("IMAGE_TOO_LARGE", 413, "Each image must be at most 10 MB");
    const definition = TYPES[file.type as keyof typeof TYPES];
    if (!definition) throw new UploadError("UNSUPPORTED_IMAGE_TYPE", 415, "Only JPEG, PNG, and WebP images are supported");
    const bytes = new Uint8Array(await file.arrayBuffer());
    if (!definition.matches(bytes)) throw new UploadError("IMAGE_TYPE_MISMATCH", 415, "Image content does not match its declared type");
    const serverFilename = `${randomUUID()}${definition.extension}`;
    validated.push({
      bytes,
      storagePath: `${projectId}/${serverFilename}`,
      originalFilename: basename(file.name.replaceAll("\\", "/")),
      mimeType: file.type as keyof typeof TYPES,
      size: file.size,
    });
  }
  const directory = projectDirectory(uploadRoot, projectId);
  await mkdir(directory, { recursive: true });
  const written: string[] = [];
  try {
    for (const item of validated) {
      const target = resolve(uploadRoot, item.storagePath);
      await (options.writeFile ?? fsWriteFile)(target, item.bytes, { flag: "wx" });
      written.push(target);
    }
  } catch (error) {
    await Promise.all(written.map((path) => unlink(path).catch(() => undefined)));
    await rmdir(directory).catch(() => undefined);
    throw error;
  }
  return validated.map((item) => ({ storagePath: item.storagePath, originalFilename: item.originalFilename, mimeType: item.mimeType, size: item.size }));
}

export async function deleteUploadedFile(uploadRoot: string, projectId: string, storagePath: string): Promise<void> {
  const directory = projectDirectory(uploadRoot, projectId);
  const target = resolve(uploadRoot, storagePath);
  const fromProject = relative(directory, target);
  if (!fromProject || fromProject.startsWith(`..${sep}`) || fromProject === ".." || isAbsolute(fromProject) || extname(target) === "") {
    throw new UploadError("UNSAFE_UPLOAD_PATH", 400, "Stored image path is outside the project upload directory");
  }
  await unlink(target).catch((error: NodeJS.ErrnoException) => {
    if (error.code !== "ENOENT") throw error;
  });
  await rmdir(directory).catch((error: NodeJS.ErrnoException) => {
    if (error.code !== "ENOENT" && error.code !== "ENOTEMPTY" && error.code !== "EEXIST") throw error;
  });
}
