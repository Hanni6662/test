import { describe, expect, it, vi } from "vitest";
import { startJsonDownload } from "./download";

describe("JSON download", () => {
  it("clicks the download and delays object URL revocation", () => {
    vi.useFakeTimers();
    const click = vi.fn(); const revoke = vi.fn();
    startJsonDownload("draft.json", { safe: true }, {
      createObjectURL: () => "blob:safe",
      revokeObjectURL: revoke,
      createAnchor: () => ({ href: "", download: "", click }),
      schedule: setTimeout,
    });
    expect(click).toHaveBeenCalledOnce();
    expect(revoke).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1_000);
    expect(revoke).toHaveBeenCalledWith("blob:safe");
    vi.useRealTimers();
  });
});
