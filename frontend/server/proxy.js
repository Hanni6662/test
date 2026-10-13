// The reverse proxy is a local trust boundary: validate BEFORE translating Origin.
export function isTrustedBrowserRequest(req, port) {
  const allowed = new Set([`http://127.0.0.1:${port}`, `http://localhost:${port}`]);
  if (!allowed.has(`http://${req.headers.host}`)) return false;
  if (req.headers['sec-fetch-site'] === 'cross-site') return false;
  const origin = req.headers.origin;
  if (origin !== undefined) return allowed.has(origin);
  if (['GET', 'HEAD'].includes(req.method)) return true;
  try { return allowed.has(new URL(req.headers.referer).origin); } catch { return false; }
}

export function localApiGuard(port) {
  return (req, res, next) => {
    if (!/^\/(api|health)(\/|$)/.test(req.url)) return next();
    if (!isTrustedBrowserRequest(req, port)) {
      res.writeHead(403, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
      return res.end(JSON.stringify({ code: 'LOCAL_ORIGIN_REJECTED', message: '本地前端仅接受同源请求' }));
    }
    if (/^\/api\//.test(req.url) && !/^\/api\/v1\//.test(req.url)) {
      res.writeHead(404); return res.end();
    }
    next();
  };
}
