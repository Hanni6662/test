import { createWriteStream, existsSync, mkdtempSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { Writable } from "node:stream";
import { MAX_BATCH_IMAGES, MAX_IMAGE_BYTES, MAX_MULTIPART_BYTES, parseMultipartImages } from "./streaming-upload";

const JPEG = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 1]);
const WEBP = new Uint8Array([0x52, 0x49, 0x46, 0x46, 1, 0, 0, 0, 0x57, 0x45, 0x42, 0x50, 1]);

function multipart(files: Array<{ name: string; type: string; bytes: Uint8Array }>, boundary = "test-boundary") {
  const encoder = new TextEncoder(); const chunks: Uint8Array[] = [];
  for (const file of files) {
    chunks.push(encoder.encode(`--${boundary}\r\nContent-Disposition: form-data; name="images"; filename="${file.name}"\r\nContent-Type: ${file.type}\r\n\r\n`), file.bytes, encoder.encode("\r\n"));
  }
  chunks.push(encoder.encode(`--${boundary}--\r\n`));
  const size = chunks.reduce((sum, chunk) => sum + chunk.length, 0); const bytes = new Uint8Array(size);
  let offset = 0; for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  return { boundary, bytes };
}

function requestFor(body: Uint8Array, boundary: string, contentLength = false): Request {
  let offset = 0;
  const stream = new ReadableStream<Uint8Array>({ pull(controller) {
    if (offset >= body.length) return controller.close();
    const end = Math.min(offset + 4096, body.length); controller.enqueue(body.slice(offset, end)); offset = end;
  } });
  return new Request("http://x/images", { method: "POST", headers: {
    "content-type": `multipart/form-data; boundary=${boundary}`,
    ...(contentLength ? { "content-length": String(body.length) } : {}),
  }, body: stream, duplex: "half" } as RequestInit & { duplex: "half" });
}

describe("streaming multipart image parser", () => {
  const roots: string[] = [];
  afterEach(() => roots.splice(0).forEach((root) => rmSync(root, { recursive: true, force: true })));

  it("streams multiple JPEG and WebP files without relying on Content-Length", async () => {
    const root = mkdtempSync(join(tmpdir(), "stream-upload-")); roots.push(root);
    const body = multipart([{ name: "a.jpg", type: "image/jpeg", bytes: JPEG }, { name: "b.webp", type: "image/webp", bytes: WEBP }]);
    const stored = await parseMultipartImages(requestFor(body.bytes, body.boundary), root, "project-1");
    expect(stored.map(({ mimeType }) => mimeType)).toEqual(["image/jpeg", "image/webp"]);
    expect(readdirSync(join(root, "project-1"))).toHaveLength(2);
  });

  it("rejects an oversized Content-Length before creating upload storage", async () => {
    const root = mkdtempSync(join(tmpdir(), "stream-upload-")); roots.push(root);
    const request = new Request("http://x/images", { method: "POST", headers: { "content-type": "multipart/form-data; boundary=x", "content-length": String(MAX_MULTIPART_BYTES + 1) }, body: "x" });
    await expect(parseMultipartImages(request, root, "project-1")).rejects.toMatchObject({ code: "UPLOAD_BATCH_TOO_LARGE" });
    expect(existsSync(join(root, "project-1"))).toBe(false);
  });

  it("accepts exactly 10 MB and rejects a chunked file one byte larger with cleanup", async () => {
    const okRoot = mkdtempSync(join(tmpdir(), "stream-upload-")); roots.push(okRoot);
    const exact = new Uint8Array(MAX_IMAGE_BYTES); exact.set(JPEG);
    const ok = multipart([{ name: "exact.jpg", type: "image/jpeg", bytes: exact }]);
    await expect(parseMultipartImages(requestFor(ok.bytes, ok.boundary), okRoot, "p")).resolves.toHaveLength(1);

    const badRoot = mkdtempSync(join(tmpdir(), "stream-upload-")); roots.push(badRoot);
    const over = new Uint8Array(MAX_IMAGE_BYTES + 1); over.set(JPEG);
    const bad = multipart([{ name: "over.jpg", type: "image/jpeg", bytes: over }]);
    await expect(parseMultipartImages(requestFor(bad.bytes, bad.boundary), badRoot, "p")).rejects.toMatchObject({ code: "IMAGE_TOO_LARGE" });
    expect(existsSync(join(badRoot, "p")) ? readdirSync(join(badRoot, "p")) : []).toEqual([]);
  });

  it("accepts exactly 20 images and rejects the 21st with cleanup", async () => {
    const root = mkdtempSync(join(tmpdir(), "stream-upload-")); roots.push(root);
    const twenty = multipart(Array.from({ length: MAX_BATCH_IMAGES }, (_, index) => ({ name: `${index}.jpg`, type: "image/jpeg", bytes: JPEG })));
    await expect(parseMultipartImages(requestFor(twenty.bytes, twenty.boundary), root, "ok")).resolves.toHaveLength(20);
    const tooMany = multipart(Array.from({ length: MAX_BATCH_IMAGES + 1 }, (_, index) => ({ name: `${index}.jpg`, type: "image/jpeg", bytes: JPEG })));
    await expect(parseMultipartImages(requestFor(tooMany.bytes, tooMany.boundary), root, "bad")).rejects.toMatchObject({ code: "TOO_MANY_IMAGES" });
    expect(existsSync(join(root, "bad")) ? readdirSync(join(root, "bad")) : []).toEqual([]);
  });

  it("cleans every newly written file when a later filesystem write fails", async () => {
    const root = mkdtempSync(join(tmpdir(), "stream-upload-")); roots.push(root);
    const body = multipart([{ name: "one.jpg", type: "image/jpeg", bytes: JPEG }, { name: "two.jpg", type: "image/jpeg", bytes: JPEG }]);
    let writers = 0;
    await expect(parseMultipartImages(requestFor(body.bytes, body.boundary), root, "p", {
      createWriter(path) {
        writers += 1;
        if (writers === 2) return new Writable({ write(_chunk, _encoding, callback) { callback(new Error("disk failure")); } });
        return createWriteStream(path, { flags: "wx" });
      },
    })).rejects.toThrow("disk failure");
    expect(existsSync(join(root, "p")) ? readdirSync(join(root, "p")) : []).toEqual([]);
  });
});
