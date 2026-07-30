#!/usr/bin/env bash
set -euo pipefail

# Rebase before every non-force push so a concurrently updated main is retained.
for attempt in 1 2 3; do
  echo "Push attempt ${attempt}/3"
  git fetch origin main
  if ! git rebase origin/main; then
    git rebase --abort || true
    echo "origin/main との競合を安全に解決できないため中止します" >&2
    exit 1
  fi
  if git push origin HEAD:main; then
    exit 0
  fi
  if [[ "$attempt" -lt 3 ]]; then sleep $((attempt * 2)); fi
done
echo "3回再試行しましたがmainへpushできませんでした" >&2
exit 1
