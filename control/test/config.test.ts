import assert from 'node:assert/strict';
import { chmodSync, mkdtempSync, readFileSync, rmSync, symlinkSync } from 'node:fs';
import { createHash, randomBytes } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { digestEvalControlConfig, resolveEvalControlConfig, validateRunBinding, EvalControlConfigError, EvalControlConfigSchema } from '../src/config.js';
import { readJobToken } from '../src/gateway_lease.js';
import { writeJobToken } from '../src/host_broker.js';

const base = {
  run: { run_id: 'run', job_config_hash: 'b'.repeat(64), config_file_sha256: 'c'.repeat(64), runtime_lock_digest: 'd'.repeat(64) },
  trialId: 'trial', sessionId: 'session', sessionRoot: 'sessions/session',
  configDigest: 'a'.repeat(64), provider: 'fixture', model: 'model',
  gatewayUrl: 'http://127.0.0.1:9000', jobTokenFile: '/tmp/token',
};

test('resolver and Cordis schema share defaults and preserve empty masks and fork zero', () => {
  const raw = { ...base, tools: { allow: [] }, lineage: { parentSessionId: 'parent', forkStep: 0 } };
  const config = resolveEvalControlConfig(raw);
  assert.deepEqual(EvalControlConfigSchema['~standard'].validate(raw), { value: config });
  assert.equal(config.bundlePath, 'bundle_descriptor.json');
  assert.equal(config.refuseAuxiliaryCalls, true);
  assert.deepEqual(config.tools?.allow, []);
  assert.equal(config.lineage?.forkStep, 0);
  for (const value of [config, config.run, config.tools, config.tools?.allow, config.lineage]) {
    assert.equal(Object.isFrozen(value), true);
  }
});

test('run binding requires exactly four own fields and a plain object', () => {
  const invalid: unknown[] = [undefined, null, [], 'run', 1, new Date(), new Map(),
    Object.create(base.run), new (class Binding { readonly run_id = 'run'; })(),
    { ...base.run, runId: 'run' }, { ...base.run, extra: true }, { ...base.run, [Symbol('extra')]: 1 },
    Object.defineProperty({ ...base.run }, 'hidden', { value: 1 }),
  ];
  for (const key of Object.keys(base.run)) {
    const missing: Record<string, unknown> = { ...base.run };
    delete missing[key];
    invalid.push(missing, { ...base.run, [key]: undefined });
  }
  for (const run_id of ['', ' run', 'run ', 'run id', 'run\tname', 'run\nname', 'run\u0000name', 'run\u0085name', 'run\u2028name', 1]) {
    invalid.push({ ...base.run, run_id });
  }
  for (const key of ['job_config_hash', 'config_file_sha256', 'runtime_lock_digest']) {
    for (const value of ['', 'a'.repeat(63), 'a'.repeat(65), 'A'.repeat(64), 'g'.repeat(64), `${'a'.repeat(64)}\n`, 1, null]) {
      invalid.push({ ...base.run, [key]: value });
    }
  }
  for (const run of invalid) {
    assert.throws(() => validateRunBinding(run), EvalControlConfigError);
    assert.throws(() => resolveEvalControlConfig({ ...base, run }), EvalControlConfigError);
    assert.ok(EvalControlConfigSchema['~standard'].validate({ ...base, run }).issues);
  }
  const { run: _run, ...legacy } = base;
  assert.throws(() => resolveEvalControlConfig(legacy), /run/);
  assert.deepEqual(validateRunBinding(Object.assign(Object.create(null), base.run)), base.run);
});

test('run validation and resolution return detached frozen copies without freezing inputs', () => {
  const raw = { ...base.run };
  const binding = validateRunBinding(raw);
  const config = resolveEvalControlConfig({ ...base, run: raw });
  assert.notEqual(binding, raw);
  assert.notEqual(config.run, raw);
  assert.equal(Object.isFrozen(raw), false);
  assert.equal(Object.isFrozen(binding), true);
  assert.equal(Object.isFrozen(config.run), true);
  for (const key of Object.keys(raw) as (keyof typeof raw)[]) raw[key] = 'changed';
  assert.deepEqual(binding, base.run);
  assert.deepEqual(config.run, base.run);
  assert.throws(() => Object.assign(binding, { run_id: 'other' }), TypeError);
});

test('resolved config digest matches canonical compact UTF-8 JSON and excludes only configDigest', () => {
  const config = resolveEvalControlConfig({
    ...base, run: { ...base.run, run_id: '运行-α' }, model: '模型',
    maxSteps: 3, maxTokens: 99, reasoningEffort: 'high',
    tools: { deny: ['write'], allow: ['read', 'write'] },
    lineage: { parentTrialId: 'parent-trial', parentSessionId: 'parent', forkStep: 0 },
  });
  const canonical = `{"bundlePath":"bundle_descriptor.json","gatewayUrl":"http://127.0.0.1:9000","jobTokenFile":"/tmp/token","lineage":{"forkStep":0,"parentSessionId":"parent","parentTrialId":"parent-trial"},"maxSteps":3,"maxTokens":99,"model":"模型","provider":"fixture","reasoningEffort":"high","refuseAuxiliaryCalls":true,"run":{"config_file_sha256":"${'c'.repeat(64)}","job_config_hash":"${'b'.repeat(64)}","run_id":"运行-α","runtime_lock_digest":"${'d'.repeat(64)}"},"sessionId":"session","sessionRoot":"sessions/session","tools":{"allow":["read","write"],"deny":["write"]},"trialId":"trial"}`;
  const expected = createHash('sha256').update(canonical, 'utf8').digest('hex');
  assert.equal(digestEvalControlConfig(config), expected);
  const reordered = {
    ...Object.fromEntries(Object.entries(config).reverse()),
    run: Object.fromEntries(Object.entries(config.run).reverse()),
    lineage: Object.fromEntries(Object.entries(config.lineage!).reverse()),
    tools: { deny: ['write'], allow: ['read', 'write'] },
  } as unknown as typeof config;
  assert.equal(digestEvalControlConfig(reordered), expected);
  assert.equal(digestEvalControlConfig({ ...config, configDigest: 'f'.repeat(64) }), expected);
  const bound = resolveEvalControlConfig({ ...config, configDigest: expected });
  assert.equal(bound.configDigest, digestEvalControlConfig(bound));
  // Resolution accepts structurally valid declarations; only the owner authenticates them.
  assert.equal(config.configDigest, base.configDigest);
  assert.notEqual(config.configDigest, expected);
  assert.notEqual(createHash('sha256').update(canonical.replace('模型', '\\u6a21\\u578b')).digest('hex'), expected);
  assert.notEqual(digestEvalControlConfig({ ...config, tools: { ...config.tools, allow: ['write', 'read'] } }), expected);
  const changes = {
    trialId: 'other-trial', sessionId: 'other-session', sessionRoot: 'other/session',
    bundlePath: 'other.json', gatewayUrl: 'http://127.0.0.1:9001', jobTokenFile: '/other-token',
    provider: 'other', model: 'other', reasoningEffort: 'low', maxSteps: 4, maxTokens: 100,
    refuseAuxiliaryCalls: false, tools: { allow: [] }, lineage: { forkStep: 1 },
  };
  for (const [key, value] of Object.entries(changes)) {
    assert.notEqual(digestEvalControlConfig(resolveEvalControlConfig({ ...config, [key]: value })), expected, key);
  }
  for (const key of Object.keys(config.run)) {
    assert.notEqual(digestEvalControlConfig(resolveEvalControlConfig({
      ...config, run: { ...config.run, [key]: key === 'run_id' ? 'other-run' : 'e'.repeat(64) },
    })), expected, key);
  }
  const defaults = resolveEvalControlConfig(base);
  assert.equal(digestEvalControlConfig(defaults), digestEvalControlConfig(resolveEvalControlConfig({
    ...base, bundlePath: 'bundle_descriptor.json', refuseAuxiliaryCalls: true,
  })));
  assert.notEqual(digestEvalControlConfig(defaults), digestEvalControlConfig(resolveEvalControlConfig({ ...base, tools: { allow: [] } })));
});

test('required gateway identity, safe budgets and known-key policy fail closed', () => {
  const invalid = [
    { gatewayUrl: undefined }, { jobTokenFile: undefined }, { providerRoutes: ['other'] },
    { maxSteps: 0 }, { maxSteps: 1.5 }, { maxTokens: Number.MAX_SAFE_INTEGER + 1 },
    { maxTokens: NaN }, { configDigest: 'A'.repeat(64) }, { sessionId: 'bad\nvalue' },
    { refuseAuxiliaryCalls: 'false' }, { tools: { allow: ['read', 'read'] } },
    { tools: { allow: ['invalid name'] } }, { tools: { unknown: [] } },
    { lineage: { parentSessionId: 'session' } }, { lineage: { forkStep: -1 } },
  ];
  for (const override of invalid) {
    const raw = { ...base, ...override };
    assert.throws(() => resolveEvalControlConfig(raw));
    assert.ok(EvalControlConfigSchema['~standard'].validate(raw).issues);
  }
});

test('session path and gateway boundary reject traversal, streams and credential-bearing URLs', () => {
  for (const sessionRoot of ['../session', 'C:session', 'C:/session', '/session', '\\\\server\\share',
    'session:stream', 'sessions/NUL.txt', 'sessions/name.', 'sessions/ name']) {
    assert.throws(() => resolveEvalControlConfig({ ...base, sessionRoot }));
  }
  assert.equal(resolveEvalControlConfig({ ...base, sessionRoot: '.\\sessions\\session' }).sessionRoot, 'sessions/session');
  for (const gatewayUrl of ['http://example.invalid', 'https://user:secret@example.invalid',
    'https://example.invalid?token=x', 'https://example.invalid#fragment', 'file:///tmp/socket']) {
    assert.throws(() => resolveEvalControlConfig({ ...base, gatewayUrl }));
  }
});

test('job token files fail closed without supported POSIX permissions', (t) => {
  const root = mkdtempSync(join(tmpdir(), 'aeval-token-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const path = join(root, 'token');
  const token = randomBytes(32).toString('hex');
  if (process.platform === 'win32') {
    assert.throws(() => writeJobToken(path, token), /POSIX/);
    assert.throws(() => readJobToken(path), /POSIX/);
    return;
  }
  writeJobToken(path, token);
  assert.equal(readJobToken(path), token);
  assert.throws(() => writeJobToken(path, token));
  chmodSync(path, 0o644);
  assert.throws(() => readJobToken(path), /0600/);
  chmodSync(path, 0o600);
  const link = join(root, 'link');
  symlinkSync(path, link);
  assert.throws(() => readJobToken(link));
});

test('manifest and lockfile pin direct imports to the frozen DSH slice', () => {
  const manifest = JSON.parse(readFileSync('package.json', 'utf8')) as {
    dependencies: Record<string, string>; devDependencies: Record<string, string>;
  };
  const lock = JSON.parse(readFileSync('package-lock.json', 'utf8')) as {
    packages: Record<string, { version?: string; integrity?: string; dependencies?: Record<string, string>; devDependencies?: Record<string, string> }>;
  };
  assert.deepEqual(lock.packages['']?.dependencies, manifest.dependencies);
  assert.deepEqual(lock.packages['']?.devDependencies, manifest.devDependencies);
  for (const [name, version] of Object.entries({ ...manifest.dependencies, ...manifest.devDependencies })) {
    assert.equal(lock.packages[`node_modules/${name}`]?.version, version);
    if (name.startsWith('@deepseek-ai/dsh')) assert.equal(version, '0.1.7-alpha.1');
  }
  // This package does NOT depend on @deepseek-ai/dsh itself — that is the
  // boundary of the neutral control plane. Its own direct imports are frozen
  // to the tarball hashes this code was built against (recorded in
  // dsh-eval-control's lockfile at 4bc6931, the move provenance), so a
  // registry drift resolves to a red test, not a silent rebuild.
  const frozen = {
    '@deepseek-ai/dsh-llm': 'sha512-DajyYU+BZqTEpfQCb21ymCqA7DBlbfcm9nguoFjY14nZ6GLdLh+ae4Nl42Nz189kVS2siu7TcDZKVy47Nub9Ag==',
    '@deepseek-ai/dsh-session': 'sha512-dUd/fUj8wign5ZuniGfr5VqMWZqFPwhc0IImapToE2NDZB/Hl0JVXtg/lS5aYoFfgxwEeIWEDjVO3UJBClQa9A==',
    '@deepseek-ai/schemastery': 'sha512-plZIZADuGLT/3PoYmUaEDZd0vG1rVahU89MevnxXsyQhFEi2D6DUuTJel7FUb3cUM9aCzqGMN99sHJe1RIushA==',
  } as const;
  for (const [name, integrity] of Object.entries(frozen)) {
    assert.equal(lock.packages[`node_modules/${name}`]?.integrity, integrity, name);
  }
});
