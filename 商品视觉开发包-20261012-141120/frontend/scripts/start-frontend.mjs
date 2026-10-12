import http from 'node:http';
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

process.chdir(fileURLToPath(new URL('../', import.meta.url)));
const openBrowser = !process.argv.includes('--no-open');
const runFile = promisify(execFile);

function readPage(url) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, res => {
      const chunks = [];
      let size = 0;
      res.on('data', chunk => {
        size += chunk.length;
        if (size > 1024 * 1024) req.destroy(new Error('端口上的服务返回了非预期内容'));
        else chunks.push(chunk);
      });
      res.on('end', () => resolve({ status: res.statusCode, html: Buffer.concat(chunks).toString('utf8') }));
      res.on('error', reject);
    });
    req.setTimeout(3000, () => req.destroy(new Error('端口上的服务没有正常响应')));
    req.on('error', reject);
  });
}

async function showPage(url) {
  if (!openBrowser) return;
  try {
    if (process.platform === 'win32') {
      // Let Windows resolve its HTTP association. Await the helper instead of
      // abandoning a detached cmd process with shell-specific argument quoting.
      await runFile('powershell.exe', [
        '-NoLogo', '-NoProfile', '-NonInteractive', '-Command',
        "$ErrorActionPreference = 'Stop'; Start-Process -FilePath $env:VISUAL_STUDIO_OPEN_URL",
      ], { windowsHide: true, timeout: 15000, env: { ...process.env, VISUAL_STUDIO_OPEN_URL: url } });
    } else {
      await runFile(process.platform === 'darwin' ? 'open' : 'xdg-open', [url], { timeout: 15000 });
    }
    console.log(`已请求系统默认浏览器打开：${url}`);
  } catch (error) {
    console.error(`无法自动打开浏览器，请手动访问：${url}`);
    console.error(error.stderr?.trim() || error.message);
    process.exitCode = 1;
  }
}

try {
  if (!existsSync('node_modules/vite/package.json')) {
    throw new Error('前端依赖尚未安装。请在 frontend 目录执行 npm.cmd ci，再双击启动脚本。');
  }
  const { createServer, loadEnv } = await import('vite');
  const env = loadEnv('development', process.cwd(), '');
  const port = Number(env.FRONTEND_PORT || 5173);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('FRONTEND_PORT 必须是 1–65535 的整数');
  const url = `http://127.0.0.1:${port}/`;
  let existing;
  try { existing = await readPage(url); }
  catch (error) { if (error.code !== 'ECONNREFUSED') throw error; }
  if (existing) {
    const isStudio = existing.status === 200 && existing.html.includes('<title>商品视觉工作台</title>') &&
      (existing.html.includes('/src/main.js') || /\/assets\/index-[^"\s]+\.js/.test(existing.html));
    if (!isStudio) throw new Error(`${port} 端口已被其他服务占用。请关闭占用程序，或在 .env.local 中修改 FRONTEND_PORT 后重试。`);
    console.log(`工作台已在运行，无需重复启动：${url}`);
    await showPage(url);
  } else {
    const server = await createServer();
    await server.listen();
    console.log('前端已启动。关闭此窗口或按 Ctrl+C 可停止本次启动的服务。');
    server.printUrls();
    for (const signal of ['SIGINT', 'SIGTERM']) {
      process.once(signal, async () => { await server.close(); process.exit(0); });
    }
    await showPage(url);
  }
} catch (error) {
  console.error(`启动失败：${error.message}`);
  process.exitCode = 1;
}
