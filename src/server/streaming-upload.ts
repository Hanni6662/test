import { randomUUID } from "node:crypto";
import { createWriteStream } from "node:fs";
import { mkdir, rmdir, unlink } from "node:fs/promises";
import { basename, resolve } from "node:path";
import { once } from "node:events";
import { pipeline } from "node:stream/promises";
import { Transform, type Writable } from "node:stream";
import Busboy from "busboy";
import { MAX_BATCH_IMAGES, MAX_IMAGE_BYTES, UploadError, type StoredUpload } from "./uploads";

export { MAX_BATCH_IMAGES, MAX_IMAGE_BYTES } from "./uploads";
export const MAX_TOTAL_IMAGE_BYTES = MAX_BATCH_IMAGES * MAX_IMAGE_BYTES;
export const MAX_MULTIPART_BYTES = MAX_TOTAL_IMAGE_BYTES + 1024 * 1024;

const MIME_EXTENSIONS = { "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp" } as const;
type SupportedMime = keyof typeof MIME_EXTENSIONS;

function matchesMagic(mime: SupportedMime, bytes: Uint8Array): boolean {
  if (mime === "image/jpeg") return bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff;
  if (mime === "image/png") return [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a].every((value, index) => bytes[index] === value);
  return String.fromCharCode(...bytes.slice(0, 4)) === "RIFF" && String.fromCharCode(...bytes.slice(8, 12)) === "WEBP";
}

function safeProjectDirectory(root: string, projectId: string): string {
  if (!/^[a-zA-Z0-9_-]{1,100}$/.test(projectId)) throw new UploadError("INVALID_PROJECT_ID", 400, "Invalid project ID");
  return resolve(root, projectId);
}

export interface StreamingUploadOptions { createWriter?: (path: string) => Writable }

export async function parseMultipartImages(request: Request, uploadRoot: string, projectId: string, options: StreamingUploadOptions = {}): Promise<StoredUpload[]> {
  const declaredLength = request.headers.get("content-length");
  if (declaredLength !== null && (!/^\d+$/.test(declaredLength) || Number(declaredLength) > MAX_MULTIPART_BYTES)) {
    throw new UploadError("UPLOAD_BATCH_TOO_LARGE", 413, "Multipart upload exceeds the total byte limit");
  }
  const contentType = request.headers.get("content-type") ?? "";
  if (!/^multipart\/form-data\s*;/i.test(contentType)) throw new UploadError("INVALID_MULTIPART", 400, "Request must use multipart form data");
  if (!request.body) throw new UploadError("NO_IMAGES", 400, "At least one image is required");

  const directory = safeProjectDirectory(uploadRoot, projectId);
  const createdPaths: string[] = [];
  const filePromises: Array<Promise<StoredUpload>> = [];
  let fileCount = 0;
  let totalImageBytes = 0;
  let fatal: Error | undefined;
  const setFatal = (error: Error) => { fatal ??= error; };

  let parser: ReturnType<typeof Busboy>;
  try {
    parser = Busboy({ headers: Object.fromEntries(request.headers), limits: { fileSize: MAX_IMAGE_BYTES + 1, files: MAX_BATCH_IMAGES + 1, fields: 0, parts: MAX_BATCH_IMAGES + 1 } });
  } catch {
    throw new UploadError("INVALID_MULTIPART", 400, "Multipart boundary is invalid");
  }
  await mkdir(directory, { recursive: true });

  parser.on("file", (fieldName, stream, info) => {
    fileCount += 1;
    if (fileCount > MAX_BATCH_IMAGES) {
      stream.resume(); setFatal(new UploadError("TOO_MANY_IMAGES", 413, "A batch may contain at most 20 images")); return;
    }
    if (fieldName !== "images") {
      stream.resume(); setFatal(new UploadError("INVALID_MULTIPART", 400, "Only the images file field is supported")); return;
    }
    const mimeType = info.mimeType as SupportedMime;
    const extension = MIME_EXTENSIONS[mimeType];
    if (!extension) {
      stream.resume(); setFatal(new UploadError("UNSUPPORTED_IMAGE_TYPE", 415, "Only JPEG, PNG, and WebP images are supported")); return;
    }
    const serverFilename = `${randomUUID()}${extension}`;
    const target = resolve(directory, serverFilename);
    createdPaths.push(target);
    let size = 0;
    let limited = false;
    const header: number[] = [];
    stream.once("limit", () => { limited = true; setFatal(new UploadError("IMAGE_TOO_LARGE", 413, "Each image must be at most 10 MB")); });
    const counter = new Transform({ transform(chunk: Buffer, _encoding, callback) {
      size += chunk.length; totalImageBytes += chunk.length;
      if (header.length < 12) header.push(...chunk.subarray(0, 12 - header.length));
      if (totalImageBytes > MAX_TOTAL_IMAGE_BYTES) {
        const error = new UploadError("UPLOAD_BATCH_TOO_LARGE", 413, "Combined image bytes exceed the batch limit"); setFatal(error); callback(error); return;
      }
      callback(null, chunk);
    } });
    filePromises.push((async () => {
      await pipeline(stream, counter, options.createWriter?.(target) ?? createWriteStream(target, { flags: "wx" }));
      if (limited || size > MAX_IMAGE_BYTES) throw new UploadError("IMAGE_TOO_LARGE", 413, "Each image must be at most 10 MB");
      if (!matchesMagic(mimeType, new Uint8Array(header))) throw new UploadError("IMAGE_TYPE_MISMATCH", 415, "Image content does not match its declared type");
      return { storagePath: `${projectId}/${serverFilename}`, originalFilename: basename(info.filename.replaceAll("\\", "/")), mimeType, size };
    })().catch((error: Error) => { setFatal(error); throw error; }));
  });
  parser.once("filesLimit", () => setFatal(new UploadError("TOO_MANY_IMAGES", 413, "A batch may contain at most 20 images")));
  parser.once("partsLimit", () => setFatal(new UploadError("TOO_MANY_IMAGES", 413, "A batch may contain at most 20 images")));

  const parserDone = new Promise<void>((resolveDone, rejectDone) => {
    parser.once("close", resolveDone); parser.once("error", rejectDone);
  });
  const reader = request.body.getReader();
  let rawBytes = 0;
  try {
    while (true) {
      if (fatal) throw fatal;
      const { done, value } = await reader.read();
      if (done) break;
      rawBytes += value.byteLength;
      if (rawBytes > MAX_MULTIPART_BYTES) throw new UploadError("UPLOAD_BATCH_TOO_LARGE", 413, "Multipart upload exceeds the total byte limit");
      if (!parser.write(Buffer.from(value))) await once(parser, "drain");
    }
    parser.end();
    await parserDone;
    const stored = await Promise.all(filePromises);
    if (fatal) throw fatal;
    if (fileCount === 0) throw new UploadError("NO_IMAGES", 400, "At least one image is required");
    return stored;
  } catch (error) {
    await reader.cancel().catch(() => undefined);
    parser.destroy();
    await Promise.allSettled(filePromises);
    await Promise.all(createdPaths.map((path) => unlink(path).catch(() => undefined)));
    await rmdir(directory).catch((cleanupError: NodeJS.ErrnoException) => {
      if (cleanupError.code !== "ENOENT" && cleanupError.code !== "ENOTEMPTY" && cleanupError.code !== "EEXIST") throw cleanupError;
    });
    throw error;
  }
}
