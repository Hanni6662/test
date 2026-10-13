import test from 'node:test';
import assert from 'node:assert/strict';
import { containerReady, serviceStages, validateDifyExecutionBudget, initializerCompletedForImage } from '../scripts/start-workbench.mjs';

test('cold start waits for database, initialization and worker before API', () => {
  const stages = serviceStages({
    api: { depends_on: { worker: {}, migrate: {} } },
    worker: { depends_on: { db: {}, migrate: {}, queue: {} } },
    migrate: { depends_on: { db: {} } },
    db: {}, queue: {},
  });
  assert.deepEqual(stages, [['db', 'queue'], ['migrate'], ['worker'], ['api']]);
});

test('Dify execution budget rejects missing, insufficient and inconsistent worker limits', () => {
  const services = (api, worker) => ({
    api: { environment: { WORKFLOW_MAX_EXECUTION_STEPS: api } },
    worker: { environment: { WORKFLOW_MAX_EXECUTION_STEPS: worker } },
  });
  assert.equal(validateDifyExecutionBudget(services('1000', '1000')), 1000);
  for (const values of [[undefined, undefined], ['500', '500'], ['1000', '800'], ['NaN', '1000'], ['2001', '2001']]) {
    assert.throws(() => validateDifyExecutionBudget(services(...values)), /Dify/);
  }
});

test('inactive optional database profiles stay excluded; cycles fail early', () => {
  assert.deepEqual(serviceStages({ api: { depends_on: { db_mysql: {}, db: {} } }, db: {} }), [['db'], ['api']]);
  assert.throws(() => serviceStages({ a: { depends_on: { b: {} } }, b: { depends_on: { a: {} } } }), /循环依赖/);
});

test('business containers wait for health, restarting/paused containers are not ready', () => {
  assert.equal(containerReady({ Status: 'running', Health: { Status: 'starting' } }, false), false);
  assert.equal(containerReady({ Status: 'running', Health: { Status: 'unhealthy' } }, false), false);
  assert.equal(containerReady({ Status: 'running', Health: { Status: 'healthy' } }, false), true);
  assert.equal(containerReady({ Status: 'running' }, false), true);
  assert.equal(containerReady({ Status: 'restarting' }, false), false);
  assert.equal(containerReady({ Status: 'paused' }, false), false);
  assert.throws(() => containerReady({ Status: 'exited', ExitCode: 1 }, false), /业务容器已退出/);
});

test('successful initializers may exit; failed initialization blocks frontend launch', () => {
  assert.equal(containerReady({ Status: 'running' }, true), false);
  assert.equal(containerReady({ Status: 'exited', ExitCode: 0 }, true), true);
  assert.throws(() => containerReady({ Status: 'exited', ExitCode: 2 }, true), /初始化容器退出码为 2/);
});

test('old successful migration is rerun for a changed release image, including a reused tag', () => {
  const container = (image, status = 'exited', exit = 0) => ({
    Image: image, State: { Status: status, ExitCode: exit },
  });
  assert.equal(initializerCompletedForImage([container('sha256:new')], 'sha256:new'), true);
  assert.equal(initializerCompletedForImage([container('sha256:old')], 'sha256:new'), false);
  assert.equal(initializerCompletedForImage([container('sha256:new', 'running')], 'sha256:new'), false);
  assert.equal(initializerCompletedForImage([container('sha256:new', 'exited', 1)], 'sha256:new'), false);
  assert.equal(initializerCompletedForImage([], 'sha256:new'), false);
  assert.equal(initializerCompletedForImage([container('sha256:new')], undefined), false);
});
