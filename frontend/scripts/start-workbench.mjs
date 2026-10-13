import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import { existsSync } from 'node:fs';
import { readFile, writeFile, unlink } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const frontend = fileURLToPath(new URL('../', import.meta.url));
const root = path.dirname(frontend);
const execute = promisify(execFile);
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

// Compose config filters services by enabled profiles; optional databases and
// vector stores that are not in the selected runtime must stay excluded.
export function serviceStages(services) {
  const remaining = new Set(Object.keys(services));
  const completed = new Set();
  const stages = [];
  while (remaining.size) {
    const stage = [...remaining].filter(name => Object.keys(services[name].depends_on || {})
      .every(dependency => !services[dependency] || completed.has(dependency)));
    if (!stage.length) throw new Error('Compose 服务存在循环依赖，无法确定启动顺序。');
    stages.push(stage);
    for (const name of stage) { remaining.delete(name); completed.add(name); }
  }
  return stages;
}

export function containerReady(state, oneShot) {
  if (oneShot) {
    if (state.Status !== 'exited') return false;
    if (state.ExitCode !== 0) throw new Error(`初始化容器退出码为 ${state.ExitCode}`);
    return true;
  }
  if (['exited', 'dead'].includes(state.Status)) throw new Error(`业务容器已退出 (exit=${state.ExitCode})`);
  return state.Status === 'running' && (!state.Health || state.Health.Status === 'healthy');
}

export function validateDifyExecutionBudget(services) {
  const limits = ['api', 'worker'].map(name => {
    const value = String(services[name]?.environment?.WORKFLOW_MAX_EXECUTION_STEPS || '');
    if (!/^[1-9]\d*$/.test(value) || Number(value) < 800 || Number(value) > 2000) {
      throw new Error(`Dify ${name} 执行步数必须显式设置为 800～2000，以覆盖 60 轮等待生图。`);
    }
    return Number(value);
  });
  if (limits[0] !== limits[1]) throw new Error('Dify API 与 Worker 执行步数配置不一致。');
  return limits[0];
}

export function initializerCompletedForImage(containers, imageId) {
  return !!imageId && containers.length > 0 && containers.every(container =>
    container.State.Status === 'exited' && container.State.ExitCode === 0 && container.Image === imageId);
}

async function run(args, label, options = {}) {
  try {
    return (await execute('docker', args, {
      cwd: frontend, windowsHide: true, timeout: 60000, maxBuffer: 32 * 1024 * 1024, ...options,
    })).stdout;
  } catch (error) {
    // Never print expanded Compose config or container environment values.
    throw new Error(`${label}失败 (${error.code || 'unknown'})。请检查 Docker Desktop、配置文件和本地镜像。`);
  }
}

async function stream(file, args, label, timeout = 0) {
  await new Promise((resolve, reject) => {
    const child = spawn(file, args, { cwd: frontend, windowsHide: true, stdio: 'inherit' });
    const timer = timeout ? setTimeout(() => { child.kill(); reject(new Error(`${label}超时。`)); }, timeout) : undefined;
    child.once('error', error => { clearTimeout(timer); reject(new Error(`${label}失败 (${error.code})。`)); });
    child.once('exit', code => {
      clearTimeout(timer);
      if (code === 0) resolve(); else reject(new Error(`${label}失败 (exit=${code})。`));
    });
  });
}

async function waitFor(label, probe, timeout) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await probe()) return;
    await delay(2000);
  }
  throw new Error(`${label}在 ${Math.round(timeout / 1000)} 秒内未就绪；浏览器未打开。`);
}

async function dockerAvailable() {
  try { await run(['info', '--format', '{{.ServerVersion}}'], 'Docker 检查', { timeout: 10000 }); return true; }
  catch { return false; }
}

async function ensureDocker(check, timeout) {
  await run(['--version'], 'Docker CLI 检查');
  if (!await dockerAvailable()) {
    const desktop = path.join(process.env.ProgramFiles || 'C:\\Program Files', 'Docker', 'Docker', 'Docker Desktop.exe');
    if (check || !existsSync(desktop)) throw new Error('Docker 引擎未运行，请先启动 Docker Desktop。');
    console.log('正在启动 Docker Desktop，等待 Docker 引擎就绪……');
    await execute('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command',
      "$ErrorActionPreference='Stop'; Start-Process -FilePath $env:WORKBENCH_DOCKER_DESKTOP -WindowStyle Hidden"],
    { windowsHide: true, timeout: 15000, env: { ...process.env, WORKBENCH_DOCKER_DESKTOP: desktop } });
    await waitFor('Docker 引擎', dockerAvailable, timeout);
  }
  await run(['compose', 'version'], 'Docker Compose 检查');
}

function requireFile(relative) {
  const absolute = path.resolve(root, relative);
  if (!existsSync(absolute)) throw new Error(`缺少配置文件：${relative}。请先完成本机环境配置。`);
  return absolute;
}

export function resolveLaunchConfig(settings, flowMode = 'standalone') {
  if (!settings || !/^localimg-[a-z0-9][a-z0-9-]{1,40}$/.test(settings.backendInstance || '')) {
    throw new Error('workbench.config.local.json 中的 backendInstance 须为自己的 localimg- 实例名。');
  }
  if (!['standalone', 'chatflow'].includes(flowMode)) throw new Error('VITE_FLOW_MODE 须为 standalone 或 chatflow。');
  const real = flowMode === 'chatflow';
  const difyProject = settings.difyProject || 'chatflow';
  if (!/^[a-z0-9][a-z0-9_-]*$/.test(difyProject)) throw new Error('difyProject 须为有效的 Compose 项目名。');
  const base = '本地部署/原图下载修复版-20261012-025431';
  return {
    real, base, instance: settings.backendInstance, difyProject,
    instanceEnv: settings.backendEnvFile || `${base}/instances/${settings.backendInstance}.env`,
    difyEnv: settings.difyEnvFile || 'chatflow/.env',
    runtimeEnv: settings.runtimeEnvFile || '本地部署/真实接线与开发交接/runtime.env',
    backendOverlay: settings.backendOverlay || '本地部署/真实接线与开发交接/backend.runtime.example.yaml',
    difyOverlay: settings.difyOverlay || '本地部署/真实接线与开发交接/dify.runtime.example.yaml',
  };
}

async function loadProject(name, envFiles, composeFiles) {
  const args = ['compose', ...envFiles.flatMap(file => ['--env-file', requireFile(file)]), '-p', name,
    ...composeFiles.flatMap(file => ['-f', requireFile(file)])];
  const config = JSON.parse(await run([...args, 'config', '--format', 'json'], `${name} 配置检查`));
  const project = { name, args, config, stages: serviceStages(config.services), original: new Map() };
  const ids = (await run(['ps', '-aq', '--filter', `label=com.docker.compose.project=${name}`], '容器检查'))
    .trim().split(/\s+/).filter(Boolean);
  if (ids.length) {
    for (const container of JSON.parse(await run(['inspect', ...ids], '容器状态检查'))) {
      if (container.Config.Labels['com.docker.compose.oneoff'] === 'True') continue;
      const service = container.Config.Labels['com.docker.compose.service'];
      if (!config.services[service]) continue;
      const existing = project.original.get(service) || [];
      existing.push(container);
      project.original.set(service, existing);
    }
  }
  return project;
}

async function waitServices(project, names, timeout) {
  await waitFor(`${project.name}: ${names.join(', ')}`, async () => {
    const ids = (await run([...project.args, 'ps', '-aq', ...names], 'Compose 容器查询'))
      .trim().split(/\s+/).filter(Boolean);
    if (!ids.length) return false;
    const containers = JSON.parse(await run(['inspect', ...ids], '容器健康检查'));
    return names.every(name => {
      const matches = containers.filter(container => container.Config.Labels['com.docker.compose.service'] === name);
      return matches.length && matches.every(container => {
        try { return containerReady(container.State, project.config.services[name].restart === 'no'); }
        catch (error) { throw new Error(`${project.name}/${name}: ${error.message}。可用 docker logs ${container.Name.slice(1)} 查看日志。`); }
      });
    });
  }, timeout);
}

async function startProject(project, timeout, restart = false) {
  console.log(`启动 ${project.name}（数据库、存储初始化及业务依赖）……`);
  for (const stage of project.stages) {
    // Successful migrations/permission initializers exit normally; do not
    // repeatedly apply them or wait for them to become long-running services.
    const pending = stage.filter(name => {
      const containers = project.original.get(name) || [];
      return !(project.config.services[name].restart === 'no' && initializerCompletedForImage(
        containers, project.imageIds.get(project.config.services[name].image)));
    });
    if (pending.length) {
      await stream('docker', [...project.args, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', ...pending],
        `${project.name} 容器启动`, timeout);
      // Reuse healthy existing services by default so active runs keep executing.
      // Restart unchanged containers only when the caller explicitly asks.
      const ids = (await run([...project.args, 'ps', '-aq', ...pending], '重启容器查询'))
        .trim().split(/\s+/).filter(Boolean);
      const originalIds = new Set(pending.filter(name => project.config.services[name].restart !== 'no')
        .flatMap(name => (project.original.get(name) || []).map(container => container.Id)));
      const restartIds = ids.filter(id => originalIds.has(id));
      if (restart && restartIds.length) {
        console.log(`重启已有容器：${pending.filter(name => project.config.services[name].restart !== 'no').join(', ')}`);
        // Services in the same dependency stage are independent. Restart them
        // concurrently so stop grace periods do not accumulate across services.
        await Promise.all(restartIds.map(id => run(['restart', '-t', '30', id], '已有容器重启', { timeout })));
      }
    }
    await waitServices(project, stage, timeout);
    console.log(`已就绪：${stage.join(', ')}`);
  }
}

async function probeJson(url, expected) {
  try {
    const response = await fetch(url, { signal: AbortSignal.timeout(5000) });
    return response.ok && (await response.json()).status === expected;
  } catch { return false; }
}

async function launch(argv) {
  const check = argv.includes('--check');
  const index = argv.indexOf('--timeout');
  const seconds = index < 0 ? 300 : Number(argv[index + 1]);
  if (!Number.isInteger(seconds) || seconds < 30 || seconds > 1800) throw new Error('--timeout 须为 30–1800 秒。');
  const timeout = seconds * 1000;
  const [major, minor] = process.versions.node.split('.').map(Number);
  if (major < 22 || (major === 22 && minor < 14)) throw new Error('需要 Node.js 22.14 或更新版本。');
  if (!existsSync(path.join(frontend, 'node_modules/vite/package.json'))) {
    if (check) throw new Error('缺少前端依赖，请执行 npm.cmd ci。');
    console.log('首次运行，正在安装前端依赖……');
    await stream(process.env.ComSpec || 'cmd.exe', ['/d', '/s', '/c', 'npm.cmd ci'], '前端依赖安装');
  }
  const { loadEnv } = await import('vite');
  const env = loadEnv('development', frontend, '');
  const frontendPort = Number(env.FRONTEND_PORT || 5173);
  if (!Number.isInteger(frontendPort) || frontendPort < 1 || frontendPort > 65535) {
    throw new Error('FRONTEND_PORT 必须是 1–65535 的整数。');
  }
  const settingsFile = path.join(frontend, 'workbench.config.local.json');
  if (!existsSync(settingsFile)) throw new Error('请复制 workbench.config.example.json 为 workbench.config.local.json，并填写自己的 backendInstance。');
  const settings = resolveLaunchConfig(JSON.parse((await readFile(settingsFile, 'utf8')).replace(/^\uFEFF/, '')), env.VITE_FLOW_MODE || 'standalone');
  const { real, base } = settings;
  // Check receiver-owned configuration before starting Docker Desktop.
  requireFile(settings.instanceEnv);
  if (real) requireFile(settings.runtimeEnv);
  await ensureDocker(check, timeout);
  const backend = await loadProject(settings.instance, [
    settings.instanceEnv, ...(real ? [settings.runtimeEnv] : []),
  ], [`${base}/compose.yaml`,
    ...(existsSync(path.join(root, base, 'images/offline-mode')) ? [`${base}/images/compose.offline.yaml`] : []),
    ...(real ? [settings.backendOverlay] : []),
  ]);
  const apiPort = backend.config.services.api.ports.find(port => port.target === 8000 && port.host_ip === '127.0.0.1')?.published;
  const backendUrl = `http://127.0.0.1:${apiPort}`;
  if (!apiPort || new URL(env.BACKEND_API_TARGET || 'http://127.0.0.1:8001').origin !== backendUrl) {
    throw new Error(`BACKEND_API_TARGET 与实例 API 端口不一致；当前实例地址应为 ${backendUrl}。请修改 .env.local。`);
  }
  const projects = real ? [await loadProject(settings.difyProject, [settings.difyEnv, settings.runtimeEnv], [
    'chatflow/compose.yaml', 'chatflow/compose.override.yaml', settings.difyOverlay,
    'frontend/scripts/workbench.dify.yaml',
  ]), backend] : [backend];
  if (real) validateDifyExecutionBudget(projects[0].config.services);
  let realNetwork;
  let imageServiceUrl;
  if (real) {
    realNetwork = backend.config.networks.real_link?.name;
    if (!realNetwork || projects[0].config.networks.real_link?.name !== realNetwork) {
      throw new Error('Dify 与后端的 REAL_LINK_NETWORK 必须一致。');
    }
    const imagePort = projects[0].config.services['image-generation'].ports.find(port => port.target === 8091 && port.host_ip === '127.0.0.1')?.published;
    if (!imagePort) throw new Error('生图服务需要发布 127.0.0.1 上的健康检查端口。');
    imageServiceUrl = `http://127.0.0.1:${imagePort}/healthz`;
  }
  // Validate all configuration and local images before restarting anything.
  for (const project of projects) {
    project.imageIds = new Map();
    await Promise.all([...new Set(Object.values(project.config.services).map(service => service.image))]
      .map(async image => {
        const id = await run(['image', 'inspect', image, '--format', '{{.Id}}'], `本地镜像 ${image} 检查`);
        project.imageIds.set(image, id.trim());
      }));
    console.log(`${project.name} 启动顺序：${project.stages.map(stage => stage.join(', ')).join(' → ')}`);
  }
  console.log('图片存储：沿用现有私有数据卷。初始化容器正常完成后退出。');
  if (check) { console.log('配置与镜像检查通过；未重启容器或打开浏览器。'); return; }
  if (real) {
    const networks = (await run(['network', 'ls', '--format', '{{.Name}}'], '网络检查')).split(/\r?\n/);
    if (!networks.includes(realNetwork)) {
      await run(['network', 'create', realNetwork], '真实业务连接网络创建');
    }
  }
  for (const project of projects) await startProject(project, timeout, argv.includes('--restart'));
  if (real) {
    const dify = projects[0];
    const expectedBudget = validateDifyExecutionBudget(dify.config.services);
    for (const service of ['api', 'worker']) {
      await run([...dify.args, 'exec', '-T', service, 'python', '-c',
        `import os; assert os.environ.get('WORKFLOW_MAX_EXECUTION_STEPS') == '${expectedBudget}'`],
      `Dify ${service} 实际执行步数配置检查`);
    }
    // Check the actual worker command line and API health, retaining the
    // portable worker/beat process healthchecks in the delivery overlay.
    for (const [service, command] of [['worker', 'worker'], ['worker_beat', 'beat']]) {
      const code = `import glob,pathlib
commands=[]
for p in glob.glob('/proc/[0-9]*/cmdline'):
    try:
        commands.append(pathlib.Path(p).read_bytes().replace(b'\\0',b' '))
    except (FileNotFoundError, PermissionError):
        pass
assert any(b'celery' in c and b' ${command} ' in c and b'python -c' not in c for c in commands)`;
      await run([...dify.args, 'exec', '-T', service, 'python', '-c', code], `Dify ${service} 进程检查`);
    }
    await run([...dify.args, 'exec', '-T', 'api', 'curl', '-fsS', 'http://127.0.0.1:5001/health'], 'Dify API 就绪检查');
    await waitFor('生图服务', () => probeJson(imageServiceUrl, 'ok'), timeout);
    // Probe the same container-to-container paths used by business execution.
    await run([...backend.args, 'exec', '-T', 'worker', 'python', '-c',
      "import httpx; c=httpx.Client(timeout=5,trust_env=False); " +
      "[c.get(u).raise_for_status() for u in " +
      "['http://visual-dify:5001/health','http://visual-image-generation:8091/healthz']]"],
    '业务 Worker 到 Dify/生图服务连接检查');
    await run([...dify.args, 'exec', '-T', 'api', 'python', '-c',
      "import httpx; c=httpx.Client(timeout=5,trust_env=False); " +
      "c.get('http://visual-backend:8000/health/ready').raise_for_status()"],
    'Dify 到业务后端回调连接检查');
  }
  await waitFor('后端 API', () => probeJson(`${backendUrl}/health/ready`, 'ready'), timeout);
  for (const project of projects) await waitServices(project, Object.keys(project.config.services), timeout);
  console.log('全部业务容器和后端 API 已就绪，正在启动前端……');
}

export async function main(argv = process.argv.slice(2)) {
  const lockPath = path.join(frontend, '.workbench-start.lock');
  let locked = false;
  try {
    if (!argv.includes('--check')) {
      if (existsSync(lockPath)) {
        const pid = Number(await readFile(lockPath, 'utf8'));
        if (!Number.isInteger(pid) || pid < 1) throw new Error('启动锁文件无效，请移除 .workbench-start.lock 后重试。');
        try { process.kill(pid, 0); throw new Error('另一个启动脚本仍在运行，请等待它完成。'); }
        catch (error) { if (error.code !== 'ESRCH') throw error; }
        await unlink(lockPath);
      }
      await writeFile(lockPath, String(process.pid), { flag: 'wx' }); locked = true;
    }
    await launch(argv);
  } finally {
    if (locked) await unlink(lockPath).catch(() => {});
  }
  if (!argv.includes('--check')) {
    await stream(process.execPath, [path.join(frontend, 'scripts/start-frontend.mjs'), '--require-backend',
      ...(argv.includes('--no-open') ? ['--no-open'] : [])], '前端启动');
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => { console.error(`启动失败：${error.message}`); process.exitCode = 1; });
}
