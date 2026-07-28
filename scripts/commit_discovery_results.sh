#!/usr/bin/env bash
set -uo pipefail

bundle="${DISCOVERY_RESULTS_BUNDLE:-.discovery-results}"
if [[ "${USE_EXISTING_DISCOVERY_BUNDLE:-false}" != true ]]; then
  rm -rf "$bundle"
  python scripts/merge_discovery_results.py capture --bundle "$bundle"
fi

git config user.name github-actions[bot]
git config user.email 41898282+github-actions[bot]@users.noreply.github.com
for attempt in 1 2 3; do
  echo "Commit attempt $attempt/3: fetch and merge onto current origin/main"
  git fetch origin main
  git reset --hard origin/main
  python scripts/merge_discovery_results.py apply --bundle "$bundle"
  python -m json.tool data/benefits.json >/dev/null
  python -m json.tool data/research-log.json >/dev/null
  python -m json.tool data/discovery-progress.json >/dev/null
  python -m json.tool data/openai-api-usage.json >/dev/null
  python -m json.tool data/official-benefit-sources.json >/dev/null
  python -m json.tool data/unresolved.json >/dev/null
  npm test
  git add data/benefits.csv data/benefits.json data/verification-queue.json data/discovery-progress.json data/research-log.json data/openai-api-usage.json data/official-benefit-sources.json data/unresolved.json
  if git diff --cached --quiet; then
    echo "committed=no" >> "${GITHUB_OUTPUT:-/dev/null}"
    exit 0
  fi
  git commit -m "chore: update OpenAI benefit discovery"
  if git push origin HEAD:main; then
    echo "committed=yes" >> "${GITHUB_OUTPUT:-/dev/null}"
    exit 0
  fi
  echo "::warning::Push rejected; preserving the bundle and retrying from the latest main"
done
echo "::error::Push failed three times. Upload the discovery-results artifact; no API rerun is needed."
exit 1
