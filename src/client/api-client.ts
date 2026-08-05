export interface ApiIssue {
  path?: Array<string | number>;
  message: string;
  code?: string;
}

export class ApiError extends Error {
  readonly name = "ApiError";

  constructor(
    public readonly code: string,
    message: string,
    public readonly status: number,
    public readonly issues: ApiIssue[] = [],
  ) {
    super(message);
  }
}

type Fetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

export class ApiClient {
  constructor(private readonly fetcher: Fetcher = (input, init) => fetch(input, init)) {}

  get<T>(path: string): Promise<T> { return this.request<T>(path); }
  post<T>(path: string, body?: unknown): Promise<T> { return this.request<T>(path, { method: "POST", body }); }
  patch<T>(path: string, body: unknown): Promise<T> { return this.request<T>(path, { method: "PATCH", body }); }
  delete<T>(path: string): Promise<T> { return this.request<T>(path, { method: "DELETE" }); }

  async upload<T>(path: string, files: File[]): Promise<T> {
    const form = new FormData();
    files.forEach((file) => form.append("images", file));
    return this.request<T>(path, { method: "POST", rawBody: form });
  }

  private async request<T>(path: string, options: { method?: string; body?: unknown; rawBody?: BodyInit } = {}): Promise<T> {
    const headers: HeadersInit = options.body === undefined ? {} : { "content-type": "application/json" };
    const response = await this.fetcher(path, {
      method: options.method ?? "GET",
      headers,
      body: options.rawBody ?? (options.body === undefined ? undefined : JSON.stringify(options.body)),
    });
    let payload: unknown;
    try { payload = await response.json(); } catch { payload = undefined; }
    if (!response.ok) {
      const structured = payload && typeof payload === "object" && "error" in payload
        ? (payload as { error?: { code?: string; message?: string; issues?: ApiIssue[] } }).error
        : undefined;
      throw new ApiError(
        structured?.code ?? `HTTP_${response.status}`,
        structured?.message ?? `请求失败（${response.status}）`,
        response.status,
        structured?.issues ?? [],
      );
    }
    return payload as T;
  }
}

export const apiClient = new ApiClient();
