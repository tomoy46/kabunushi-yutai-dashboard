const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const workflowPath = path.join(__dirname, '..', '.github', 'workflows', 'discover-benefits-with-openai.yml');
const workflow = fs.readFileSync(workflowPath, 'utf8');

test('normal workflow runs persist verified results', () => {
  assert.match(
    workflow,
    /diagnostic_mode: \{description: 'Diagnose code 1301 without changing data', default: false, type: boolean\}/,
  );
  assert.match(workflow, /name: Commit verified results\n\s+if: \$\{\{ success\(\) && inputs\.diagnostic_mode != true \}\}/);
});

test('diagnostic mode is enabled only by the checked input', () => {
  assert.match(workflow, /\$\{\{ inputs\.diagnostic_mode == true && '--diagnostic-mode' \|\| '' \}\}/);
  assert.doesNotMatch(workflow, /\$\{\{ inputs\.diagnostic_mode && '--diagnostic-mode'/);
});

test('all boolean workflow inputs use typed boolean comparisons', () => {
  for (const [input, option] of [
    ['retry_failed', '--retry-failed'],
    ['official_only', '--official-only'],
    ['diagnostic_mode', '--diagnostic-mode'],
  ]) {
    assert.match(workflow, new RegExp(`inputs\\.${input} == true && '${option}'`));
  }
});

test('commit step reports staged changes and explains an empty diff', () => {
  assert.match(workflow, /added=\$\{added\} updated=\$\{updated\}/);
  assert.match(workflow, /git diff --cached --numstat/);
  assert.match(workflow, /::warning::No data files changed/);
  assert.match(workflow, /Production targets and Production summary logs/);
});

test('production inputs and startup log are wired to workflow_dispatch values', () => {
  for (const [input, option] of [
    ['batch_size', '--batch-size'],
    ['daily_limit', '--daily-limit'],
    ['start_code', '--start-code'],
    ['end_code', '--end-code'],
  ]) {
    assert.match(workflow, new RegExp(`\\$\\{\\{ inputs\\.${input}[^\\n]*${option}|${option}[^\\n]*\\$\\{\\{ inputs\\.${input}`));
  }

  for (const input of ['diagnostic_mode', 'start_code', 'end_code', 'batch_size', 'daily_limit']) {
    assert.match(workflow, new RegExp(`echo "${input}=\\$\\{[A-Z_]+\\}"`));
  }
});
