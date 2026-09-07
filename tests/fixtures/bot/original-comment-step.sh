set -euo pipefail
pr="$(cat pr-number.txt)"
# The renderer already emits this marker as its first line, so the
# comment is matched on what the report actually contains rather than
# on a second marker this workflow would have to keep in sync.
marker='<!-- vaws-knowledge-review-bot:v1 -->'
body="$(cat gate-comment.md)"
head -n1 gate-comment.md | grep -qF "$marker" || {
  echo "the report no longer starts with the expected marker; refusing to guess" >&2
  exit 1
}

# Update in place rather than posting again. The rendering is
# deterministic (pinned by tests/test_bot_gates.py), so an unchanged
# result produces an unchanged comment instead of a fresh notification
# that teaches reviewers to ignore the comment.
existing="$(gh api "repos/$REPO/issues/$pr/comments" --paginate \
  --jq "map(select(.body | startswith(\"$marker\"))) | .[0].id // empty")"

if [ -n "$existing" ]; then
  gh api -X PATCH "repos/$REPO/issues/comments/$existing" -f body="$body" >/dev/null
  echo "updated comment $existing"
else
  gh api -X POST "repos/$REPO/issues/$pr/comments" -f body="$body" >/dev/null
  echo "posted a new comment"
fi
