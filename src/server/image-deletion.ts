import { randomUUID } from "node:crypto";
import { mkdir, readdir, rename, rmdir, unlink } from "node:fs/promises";
import { isAbsolute, relative, resolve, sep } from "node:path";
import { ServiceError, type PersistedReferenceImage, type PromptBuilderService } from "./persistence/service";

export interface ImageDeletionOptions {
  unlinkFile?: (path: string) => Promise<void>;
  removeDirectory?: (path: string) => Promise<void>;
}

const CLEANUP_FILE_PATTERN = /^cleanup-[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\.delete$/i;

async function removeIfEmpty(path: string, removeDirectory: (path: string) => Promise<void>): Promise<boolean> {
  try {
    await removeDirectory(path);
    return true;
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    return code === "ENOENT" || code === "ENOTEMPTY" || code === "EEXIST";
  }
}

function resolveUploadedPath(uploadRoot: string, image: PersistedReferenceImage): { projectRoot: string; originalPath: string } {
  if (!image.uploadBacked || image.storagePath === null) throw new ServiceError("IMAGE_NOT_UPLOAD_BACKED", 409, "Image has no managed upload file");
  const projectRoot = resolve(uploadRoot, image.projectId);
  const originalPath = resolve(uploadRoot, image.storagePath);
  const inside = relative(projectRoot, originalPath);
  if (!inside || inside === ".." || inside.startsWith(`..${sep}`) || isAbsolute(inside)) throw new ServiceError("UNSAFE_UPLOAD_PATH", 400, "Stored image path is outside the project upload directory");
  return { projectRoot, originalPath };
}

export async function sweepImageDeletionQuarantine(projectRoot: string): Promise<void> {
  const quarantineDirectory = resolve(projectRoot, ".quarantine");
  let entries;
  try {
    entries = await readdir(quarantineDirectory, { withFileTypes: true });
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") return;
    return;
  }
  await Promise.all(entries
    .filter((entry) => entry.isFile() && CLEANUP_FILE_PATTERN.test(entry.name))
    .map((entry) => unlink(resolve(quarantineDirectory, entry.name)).catch(() => undefined)));
  await removeIfEmpty(quarantineDirectory, rmdir);
}

export async function deleteReferenceImageConsistently(
  service: Pick<PromptBuilderService, "getReferenceImage" | "deleteReferenceImage">,
  imageId: string,
  uploadRoot: string,
  options: ImageDeletionOptions = {},
): Promise<{ deleted: true; cleanupPending: boolean }> {
  const image = service.getReferenceImage(imageId);
  if (!image.uploadBacked) {
    service.deleteReferenceImage(imageId);
    return { deleted: true, cleanupPending: false };
  }

  const { projectRoot, originalPath } = resolveUploadedPath(uploadRoot, image);
  const quarantineDirectory = resolve(projectRoot, ".quarantine");
  await sweepImageDeletionQuarantine(projectRoot);
  const deletionId = randomUUID();
  const quarantinePath = resolve(quarantineDirectory, `active-${deletionId}.delete`);
  const cleanupPath = resolve(quarantineDirectory, `cleanup-${deletionId}.delete`);
  await mkdir(quarantineDirectory, { recursive: true });
  await rename(originalPath, quarantinePath);
  try {
    service.deleteReferenceImage(imageId);
  } catch (error) {
    try {
      await rename(quarantinePath, originalPath);
    } catch {
      throw new ServiceError("IMAGE_DELETE_RECOVERY_FAILED", 500, "Image deletion failed and file recovery was unsuccessful");
    }
    await removeIfEmpty(quarantineDirectory, options.removeDirectory ?? rmdir);
    throw error;
  }

  try {
    await rename(quarantinePath, cleanupPath);
  } catch {
    return { deleted: true, cleanupPending: true };
  }
  try {
    await (options.unlinkFile ?? unlink)(cleanupPath);
  } catch {
    return { deleted: true, cleanupPending: true };
  }
  const removeDirectory = options.removeDirectory ?? rmdir;
  const quarantineClean = await removeIfEmpty(quarantineDirectory, removeDirectory);
  const projectClean = await removeIfEmpty(projectRoot, removeDirectory);
  return { deleted: true, cleanupPending: !quarantineClean || !projectClean };
}
