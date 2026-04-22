#!/usr/bin/env bash
set -euo pipefail

event_name="${CI_EVENT_NAME:-${GITHUB_EVENT_NAME:-}}"
pr_base_sha="${PR_BASE_SHA:-}"
pr_head_sha="${PR_HEAD_SHA:-${GITHUB_SHA:-HEAD}}"
push_before_sha="${PUSH_BEFORE_SHA:-}"
head_sha="${GITHUB_SHA:-HEAD}"

base_sha="$push_before_sha"
compare_sha="$head_sha"

if [[ "$event_name" == "pull_request" || "$event_name" == "pull_request_target" ]]; then
  base_sha="$pr_base_sha"
  compare_sha="$pr_head_sha"
fi

zero_sha='0000000000000000000000000000000000000000'
if [[ -z "$base_sha" || "$base_sha" == "$zero_sha" ]]; then
  if git rev-parse --verify "${compare_sha}^" >/dev/null 2>&1; then
    base_sha="${compare_sha}^"
  else
    base_sha="$compare_sha"
  fi
fi

changed_file_list="$(mktemp)"
if git rev-parse --verify "${base_sha}^{commit}" >/dev/null 2>&1; then
  git diff --name-only "$base_sha" "$compare_sha" >"$changed_file_list"
else
  git diff-tree --no-commit-id --name-only -r "$compare_sha" >"$changed_file_list"
fi

is_docs_path() {
  case "$1" in
    README.md|docs/*|docs/build_docs.py|tests/unit/test_docs_command_alignment.py|tests/integration/test_docs_export_and_links.py|.github/workflows/ci-docs.yml)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

docs_changed=false
non_docs_changed=false

while IFS= read -r path; do
  [[ -z "$path" ]] && continue
  if is_docs_path "$path"; then
    docs_changed=true
  else
    non_docs_changed=true
  fi
done <"$changed_file_list"

docs_only=false
if [[ "$docs_changed" == true && "$non_docs_changed" == false ]]; then
  docs_only=true
fi

{
  echo "base_sha=$base_sha"
  echo "compare_sha=$compare_sha"
  echo "docs_changed=$docs_changed"
  echo "docs_only=$docs_only"
} >>"${GITHUB_OUTPUT:-/dev/stdout}"
