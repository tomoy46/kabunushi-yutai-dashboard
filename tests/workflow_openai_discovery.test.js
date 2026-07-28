const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const test = require('node:test');

const workflowPath = path.join(__dirname, '..', '.github', 'workflows', 'discover-benefits-with-openai.yml');
const packagePath = path.join(__dirname, '..', 'package.json');
const parsedWorkflow = JSON.parse(execFileSync(
  'ruby',
  ['-ryaml', '-rjson', '-e', 'puts JSON.generate(YAML.safe_load(File.read(ARGV[0]), aliases: true))', workflowPath],
  { encoding: 'utf8' },
));
// YAML 1.1 parsers treat the unquoted `on` key as a boolean. Support both
// interpretations so these tests inspect the parsed workflow, not its layout.
const triggers = parsedWorkflow.on || parsedWorkflow.true;
const dispatchInputs = triggers.workflow_dispatch.inputs;
const steps = parsedWorkflow.jobs.discover.steps;
const stepNamed = (name) => steps.find((step) => step.name === name);

test('production workflow commits verified results after a partial failure', () => {
  assert.equal(dispatchInputs.diagnostic_mode.default, false);
  assert.equal(
    stepNamed('Commit verified results').if,
    '${{ success() && inputs.diagnostic_mode != true }}',
  );
});

test('diagnostic mode is enabled only by the checked input', () => {
  const command = stepNamed('Discover from official sources').run.replace(/[\s'"]/g, '');
  assert.ok(command.includes('${{inputs.diagnostic_mode==true&&--diagnostic-mode||}}'));
  assert.ok(!command.includes('${{inputs.diagnostic_mode&&--diagnostic-mode'));
});

test('all boolean workflow inputs use typed boolean comparisons', () => {
  const command = stepNamed('Discover from official sources').run.replace(/[\s'"]/g, '');
  for (const [input, option] of [
    ['retry_failed', '--retry-failed'],
    ['official_only', '--official-only'],
    ['diagnostic_mode', '--diagnostic-mode'],
  ]) {
    assert.ok(command.includes(`inputs.${input}==true&&${option}`));
  }
});

test('commit step reports staged changes and explains an empty diff', () => {
  const command = stepNamed('Commit verified results').run;
  assert.ok(command.includes('added={added} updated={updated}'));
  assert.ok(command.includes('git diff --cached --numstat'));
  assert.ok(command.includes('::warning::No data files changed'));
  assert.ok(command.includes('No commit was created'));
});

test('tests run before discovery and a failed test blocks API use and commit', () => {
  const installIndex = steps.findIndex((step) => step.name === 'Install and verify PDF text extraction');
  const testIndex = steps.findIndex((step) => step.name === 'Test before using the production API');
  const discoveryIndex = steps.findIndex((step) => step.name === 'Discover from official sources');
  const commitIndex = steps.findIndex((step) => step.name === 'Commit verified results');
  assert.ok(installIndex > 0 && installIndex < testIndex && testIndex < discoveryIndex && discoveryIndex < commitIndex);
  const install = stepNamed('Install and verify PDF text extraction').run;
  assert.ok(install.includes('sudo apt-get update'));
  assert.ok(install.includes('sudo apt-get install -y poppler-utils'));
  assert.ok(install.includes('pdftotext -v'));
  assert.equal(stepNamed('Commit verified results').if, '${{ success() && inputs.diagnostic_mode != true }}');
});

test('live source verification allows one stale issuer before production discovery', () => {
  const liveCheck = stepNamed('Verify maintained official sources over real HTTP');
  assert.equal(liveCheck.env.RUN_LIVE_OFFICIAL_SOURCES, '1');
  assert.ok(liveCheck.run.includes('test_live_official_sources.py'));
  assert.equal(liveCheck['continue-on-error'], undefined);
});

test('preflight is offline and excludes the opt-in live official-source suite', () => {
  const preflight = stepNamed('Test before using the production API');
  const testCommand = JSON.parse(require('node:fs').readFileSync(packagePath, 'utf8')).scripts.test;
  assert.equal(preflight.run, 'npm test');
  assert.equal(preflight.env, undefined);
  assert.ok(!testCommand.includes('test_live_official_sources.py'));
  assert.ok(!testCommand.includes('RUN_LIVE_OFFICIAL_SOURCES'));
  assert.ok(testCommand.includes('tests/run_unit_tests.py'));
});

test('workflow reports all saved outcomes and commit status', () => {
  const report = stepNamed('Report workflow outcome');
  assert.equal(report.if, '${{ always() }}');
  const command = report.run;
  for (const text of ['confirmed saved=', 'research-log saved=', 'failed=', 'git committed=']) {
    assert.ok(command.includes(text));
  }
  assert.equal(report.env.OPENAI_CALLS, '${{ steps.discovery.outputs.openai_calls }}');
  assert.equal(report.env.ZERO_CONFIRMED_CAUSE, '${{ steps.discovery.outputs.zero_confirmed_cause }}');
  assert.ok(command.includes('confirmed=0; OpenAI calls='));
});

test('a successful data commit deploys and verifies GitHub Pages without relying on push chaining', () => {
  assert.equal(parsedWorkflow.permissions.contents, 'write');
  assert.equal(parsedWorkflow.permissions.pages, 'write');
  assert.equal(parsedWorkflow.permissions['id-token'], 'write');
  assert.equal(parsedWorkflow.jobs.discover.environment.name, 'github-pages');

  for (const [name, action] of [
    ['Configure GitHub Pages', 'actions/configure-pages@v5'],
    ['Upload GitHub Pages artifact', 'actions/upload-pages-artifact@v3'],
    ['Deploy GitHub Pages', 'actions/deploy-pages@v4'],
  ]) {
    const step = stepNamed(name);
    assert.equal(step.if, "${{ steps.commit_results.outputs.committed == 'yes' }}");
    assert.equal(step.uses, action);
  }

  const verify = stepNamed('Verify deployed shareholder benefits');
  assert.equal(verify.if, "${{ steps.commit_results.outputs.committed == 'yes' }}");
  assert.ok(verify.run.includes("{'7550', '7616', '7412'}"));
  assert.ok(verify.run.includes('confirmed != 14'));
  assert.ok(verify.run.includes('/data/benefits.json?v='));
});

test('production inputs and startup log are wired to workflow_dispatch values', () => {
  const command = stepNamed('Discover from official sources').run.replace(/[\s'"]/g, '');
  const expectedWiring = {
    batch_size: '--batch-size${{inputs.batch_size}}',
    daily_limit: '--daily-limit${{inputs.daily_limit}}',
    security_codes: '--security-codes${{inputs.security_codes}}',
    retry_failed: '${{inputs.retry_failed==true&&--retry-failed||}}',
    official_only: '${{inputs.official_only==true&&--official-only||}}',
    diagnostic_mode: '${{inputs.diagnostic_mode==true&&--diagnostic-mode||}}',
  };
  for (const [input, commandFragment] of Object.entries(expectedWiring)) {
    assert.ok(command.includes(commandFragment), `${input} is wired to its production option`);
  }

  const logStep = stepNamed('Log workflow inputs');
  for (const input of ['diagnostic_mode', 'security_codes', 'batch_size', 'daily_limit']) {
    const environmentName = input.toUpperCase();
    assert.equal(logStep.env[environmentName], `\${{ inputs.${input} }}`);
    assert.ok(logStep.run.includes(`echo "${input}=\${${environmentName}}"`));
  }
});

test('deprecated security-code range inputs cannot trigger indiscriminate discovery', () => {
  assert.equal(dispatchInputs.start_code, undefined);
  assert.equal(dispatchInputs.end_code, undefined);
  const command = stepNamed('Discover from official sources').run;
  assert.ok(!command.includes('--start-code'));
  assert.ok(!command.includes('--end-code'));
});
