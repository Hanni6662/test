import { defineConfig, loadEnv } from 'vite';
import vue from '@vitejs/plugin-vue';
import { localApiGuard } from './server/proxy.js';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const target = env.BACKEND_API_TARGET || 'http://127.0.0.1:8001';
  const origin = env.BACKEND_TRUSTED_ORIGIN || new URL(target).origin;
  const port = Number(env.FRONTEND_PORT || 5173);
  const proxy = Object.fromEntries(['/api/v1', '/health'].map(path => [path, {
    target, changeOrigin: true, timeout: 180000, proxyTimeout: 180000,
    configure(server) {
      server.on('proxyReq', (outgoing, incoming) => {
        // Only same-origin requests reach here. Never expose internal services.
        if (!['GET', 'HEAD'].includes(incoming.method)) {
          outgoing.setHeader('Origin', origin);
          outgoing.setHeader('Referer', `${origin}/`);
        }
      });
      server.on('error', (_error, _req, res) => {
        if (res && !res.headersSent && res.writeHead) {
          res.writeHead(502, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
          res.end(JSON.stringify({ code: 'BACKEND_OFFLINE', message: '无法连接后端，请检查服务地址及运行状态' }));
        }
      });
    },
  }]));
  const guard = {
    name: 'local-api-origin-guard',
    configureServer(server) { server.middlewares.use(localApiGuard(port)); },
    configurePreviewServer(server) { server.middlewares.use(localApiGuard(port)); },
  };
  return {
    plugins: [guard, vue()],
    server: { host: '127.0.0.1', port, strictPort: true, cors: false, proxy },
    preview: { host: '127.0.0.1', port, strictPort: true, cors: false, proxy },
  };
});
