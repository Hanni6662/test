export class ApiError extends Error {
  constructor(message, status = 0, code = 'NETWORK_ERROR', requestId = '') {
    super(message); this.status = status; this.code = code; this.requestId = requestId;
  }
}
export class StaleIdentityError extends Error {}
let generation = 0;
const listeners = new Set();
export function invalidateIdentity() { generation++; for (const fn of listeners) fn(); }
export const onInvalidIdentity = fn => { listeners.add(fn); return () => listeners.delete(fn); };
export const identityGeneration = () => generation;

export async function request(path, { method = 'GET', body, key, binary = false, timeout = 60000 } = {}) {
  const epoch = generation;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const headers = {};
    if (key) headers['Idempotency-Key'] = key;
    if (body !== undefined && !(body instanceof FormData)) headers['Content-Type'] = 'application/json';
    const response = await fetch(`/api/v1${path}`, {
      method, headers, credentials: 'include', cache: 'no-store', signal: controller.signal,
      body: body === undefined || body instanceof FormData ? body : JSON.stringify(body),
    });
    if (epoch !== generation) throw new StaleIdentityError();
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      if (epoch !== generation) throw new StaleIdentityError();
      if (response.status === 401) invalidateIdentity();
      throw new ApiError(payload.message || '请求失败，请稍后再试', response.status, payload.code, payload.request_id);
    }
    const data = response.status === 204 ? null : binary ? await response.blob() : await response.json();
    if (epoch !== generation) throw new StaleIdentityError();
    return binary ? { blob: data, headers: response.headers } : data;
  } catch (error) {
    if (epoch !== generation && !(error instanceof ApiError && error.status === 401)) throw new StaleIdentityError();
    if (error instanceof ApiError || error instanceof StaleIdentityError) throw error;
    throw new ApiError(error.name === 'AbortError' ? '请求超时，请检查原操作结果后再试' : '网络连接中断，请检查后端服务');
  } finally { clearTimeout(timer); }
}

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
// Shared storage contains only a short-lived lock token, never session credentials.
async function leaseLock(fn) {
  const storageKey = 'visual-studio:cookie-lock';
  const token = crypto.randomUUID();
  const deadline = Date.now() + 75000;
  const read = () => { try { return JSON.parse(localStorage.getItem(storageKey)); } catch { return null; } };
  while (Date.now() < deadline) {
    const previous = read();
    if (!previous || previous.expires < Date.now()) {
      localStorage.setItem(storageKey, JSON.stringify({ token, expires: Date.now() + 15000 }));
      await delay(80 + Math.random() * 100);
      if (read()?.token === token) {
        const heartbeat = setInterval(() => {
          if (read()?.token === token) localStorage.setItem(storageKey, JSON.stringify({ token, expires: Date.now() + 15000 }));
        }, 3000);
        try { return await fn(); } finally {
          clearInterval(heartbeat);
          if (read()?.token === token) localStorage.removeItem(storageKey);
        }
      }
    }
    await delay(150 + Math.random() * 150);
  }
  throw new Error('另一标签页正在更新会话，请稍后再试');
}
export function cookieLock(fn) {
  return navigator.locks?.request ? navigator.locks.request('visual-studio:guest-session', fn) : leaseLock(fn);
}
