const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const test = require('node:test');

const workflowPath = path.join(__dirname, '..', '.github', 'workflows', 'discover-benefits-with-openai.yml');
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

test('normal workflow runs persist verified results', () => {
  assert.equal(dispatchInputs.diagnostic_mode.default, false);
  assert.equal(stepNamed('Commit verified results').if, '${{ success() && inputs.diagnostic_mode != true }}');
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
  assert.ok(command.includes('Production targets and Production summary logs'));
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
