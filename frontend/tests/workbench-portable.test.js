import test from 'node:test';
import assert from 'node:assert/strict';
import { resolveLaunchConfig } from '../scripts/start-workbench.mjs';

test('receiver instance selects its own environment file and standalone runtime', () => {
  const config = resolveLaunchConfig({ backendInstance: 'localimg-receiver01' }, 'standalone');
  assert.equal(config.instance, 'localimg-receiver01');
  assert.equal(config.real, false);
  assert.ok(config.instanceEnv.endsWith('/instances/localimg-receiver01.env'));
  assert.equal(config.instanceEnv, '本地部署/原图下载修复版-20261012-025431/instances/localimg-receiver01.env');
  assert.ok(config.runtimeEnv.startsWith('本地部署/真实接线与开发交接/'));
});

test('chatflow uses receiver-owned paths and Compose project without a fallback', () => {
  const config = resolveLaunchConfig({
    backendInstance: 'localimg-team01', difyProject: 'team-dify',
    backendEnvFile: 'configuration/backend.env', difyEnvFile: 'configuration/dify.env',
    runtimeEnvFile: 'configuration/runtime.env',
    backendOverlay: 'configuration/backend.yaml', difyOverlay: 'configuration/dify.yaml',
  }, 'chatflow');
  assert.equal(config.real, true);
  assert.equal(config.difyProject, 'team-dify');
  assert.equal(config.instanceEnv, 'configuration/backend.env');
  assert.equal(config.runtimeEnv, 'configuration/runtime.env');
  assert.equal(config.backendOverlay, 'configuration/backend.yaml');
  assert.equal(config.difyOverlay, 'configuration/dify.yaml');
});

test('invalid receiver configuration and flow mode fail before Docker operations', () => {
  assert.throws(() => resolveLaunchConfig({}), /backendInstance/);
  assert.throws(() => resolveLaunchConfig({ backendInstance: '../old-project' }), /backendInstance/);
  assert.throws(() => resolveLaunchConfig({ backendInstance: 'localimg-team01', difyProject: 'invalid project' }), /difyProject/);
  assert.throws(() => resolveLaunchConfig({ backendInstance: 'localimg-team01' }, 'automatic'), /VITE_FLOW_MODE/);
});
