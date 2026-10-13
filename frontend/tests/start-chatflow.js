process.env.VITE_FLOW_MODE = 'chatflow';
process.env.FRONTEND_PORT = process.env.WORKBENCH_TEST_PORT || '5174';
const { createServer } = await import('vite');
const server = await createServer();
await server.listen();
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, async () => { await server.close(); process.exit(0); });
