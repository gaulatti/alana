import assert from 'node:assert/strict';
import {execFileSync, spawnSync} from 'node:child_process';
import {mkdtempSync, mkdirSync, readFileSync, writeFileSync, existsSync} from 'node:fs';
import {tmpdir} from 'node:os';
import path from 'node:path';
import test from 'node:test';
import {parse} from 'yaml';
import {evaluate} from './workflow-expressions.mjs';

const workflow = parse(readFileSync(new URL('../workflows/ci.yml', import.meta.url), 'utf8'));
const step = (job, name) => {
  const found = workflow.jobs[job].steps.filter(candidate => candidate.name === name);
  assert.equal(found.length, 1, `${job}: step "${name}"`);
  return found[0];
};
const BASELINE = 'Measure base revision coverage for changed-line mode';
const SELECT = 'Select accepted coverage configuration';
const ENFORCE = 'Enforce 100% line and branch coverage';
const base = 'b'.repeat(40);

function inputs(event, {pullRequestBase = '', before = ''} = {}) {
  const github = {event_name: event, event: {before, ...(pullRequestBase ? {pull_request: {base: {sha: pullRequestBase}}} : {})}};
  const render = expression => evaluate(expression, {github}).coerceString();
  const withInputs = step('verify', ENFORCE).with;
  return {baseRef: render(withInputs['coverage-base-ref']), baseline: render(withInputs['coverage-baseline'])};
}

test('the required coverage step keeps its name and passes the base configuration, base ref and baseline', () => {
  const enforce = step('verify', ENFORCE);
  assert.match(enforce.uses, /clouseau-ci/);
  assert.equal(enforce.with.operation, 'coverage');
  assert.equal(enforce.with.config, '${{ runner.temp }}/clouseau-config.json');
  assert.equal(enforce.with['coverage-format'], 'coveragepy');
  assert.equal(enforce.with['coverage-report'], 'coverage/coverage.json');
  assert.equal(enforce.with['source-directory'], '.');
  assert.deepEqual(inputs('pull_request', {pullRequestBase: base}), {baseRef: base, baseline: 'coverage/baseline.json'});
  assert.deepEqual(inputs('push', {before: base}), {baseRef: base, baseline: 'coverage/baseline.json'});
});

test('a manual run passes no base ref or baseline, so the engine applies the stricter exact gate', () => {
  assert.deepEqual(inputs('workflow_dispatch'), {baseRef: '', baseline: ''});
});

test('the unit and verify checkouts fetch the history that changed-line coverage diffs against', () => {
  for (const job of ['unit', 'verify']) {
    const checkout = workflow.jobs[job].steps.find(candidate => candidate.uses?.startsWith('actions/checkout@') && !candidate.with?.repository);
    assert.equal(checkout.with['fetch-depth'], 0, job);
  }
});

test('the baseline is uploaded with the coverage artifact that verify downloads', () => {
  const upload = workflow.jobs.unit.steps.find(candidate => candidate.uses?.startsWith('actions/upload-artifact@'));
  assert.match(upload.with.path, /coverage\/baseline\.json/);
});

function repository() {
  const dir = mkdtempSync(path.join(tmpdir(), 'alana-coverage-'));
  const git = (...args) => execFileSync('git', ['-c', 'user.name=t', '-c', 'user.email=t@t', ...args], {cwd: dir, encoding: 'utf8'}).trim();
  git('init', '-q');
  mkdirSync(path.join(dir, '.github'));
  return {dir, git};
}

function run(script, dir, env) {
  return spawnSync('bash', ['-eo', 'pipefail', '-c', script], {cwd: dir, encoding: 'utf8', env: {PATH: process.env.PATH, RUNNER_TEMP: path.join(dir, 'tmp'), GITHUB_WORKSPACE: dir, ...env}});
}

test('the baseline step skips with a clear message, and succeeds, when there is no base revision', () => {
  const {dir} = repository();
  mkdirSync(path.join(dir, 'tmp'));
  const result = run(step('unit', BASELINE).run, dir, {BASE_SHA: ''});
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /No base revision .*skipping .*exact coverage gate applies/);
  assert.equal(existsSync(path.join(dir, 'tmp', 'clouseau-config.json')), false);
  assert.equal(existsSync(path.join(dir, 'coverage', 'baseline.json')), false);
});

test('verify takes the coverage configuration from the base revision, never from the pull request', () => {
  const {dir, git} = repository();
  mkdirSync(path.join(dir, 'tmp'));
  writeFileSync(path.join(dir, '.github', 'clouseau.json'), '{"version":2}\n');
  git('add', '.'); git('commit', '-q', '-m', 'base');
  const baseSha = git('rev-parse', 'HEAD');
  writeFileSync(path.join(dir, '.github', 'clouseau.json'), '{"version":2,"coverage":{"mode":"changed_lines"}}\n');
  git('commit', '-q', '-am', 'head');
  const result = run(step('verify', SELECT).run, dir, {BASE_SHA: baseSha});
  assert.equal(result.status, 0, result.stderr);
  assert.equal(readFileSync(path.join(dir, 'tmp', 'clouseau-config.json'), 'utf8'), '{"version":2}\n');
});
