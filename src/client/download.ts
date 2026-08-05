interface DownloadAnchor {
  href: string;
  download: string;
  click(): void;
}

interface DownloadDependencies {
  createObjectURL(blob: Blob): string;
  revokeObjectURL(url: string): void;
  createAnchor(): DownloadAnchor;
  schedule(callback: () => void, delay: number): unknown;
}

const browserDependencies = (): DownloadDependencies => ({
  createObjectURL: (blob) => URL.createObjectURL(blob),
  revokeObjectURL: (url) => URL.revokeObjectURL(url),
  createAnchor: () => document.createElement("a"),
  schedule: (callback, delay) => setTimeout(callback, delay),
});

export function startJsonDownload(name: string, value: unknown, dependencies = browserDependencies()): void {
  const url = dependencies.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }));
  const anchor = dependencies.createAnchor();
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  dependencies.schedule(() => dependencies.revokeObjectURL(url), 1_000);
}
