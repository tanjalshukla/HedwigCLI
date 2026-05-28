#!/usr/bin/env bash
# Hedwig booth — pre-demo setup.
# Run from the repo root or from demo_recipe_api/.
# Stops on any failure so a broken setup doesn't go unnoticed.

set -euo pipefail

PROFILE="${AWS_PROFILE:-dev}"
REGION="${AWS_REGION:-us-east-1}"

KEEP_STATE=0
for arg in "$@"; do
  case "$arg" in
    --keep-state) KEEP_STATE=1 ;;
    -h|--help)
      echo "Usage: $(basename "$0") [--keep-state]"
      echo "  --keep-state   preserve trust.db and recipe_api/ edits (mid-day re-run)"
      exit 0
      ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

# Resolve repo root regardless of where the script is invoked from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEMO_DIR="$REPO_ROOT/demo_recipe_api"
VENV_PY="$REPO_ROOT/.venv/bin/python"

cyan()  { printf "\033[36m%s\033[0m\n" "$1"; }
green() { printf "\033[32m%s\033[0m\n" "$1"; }
yellow(){ printf "\033[33m%s\033[0m\n" "$1"; }
red()   { printf "\033[31m%s\033[0m\n" "$1"; }

cyan "→ Hedwig demo setup"
echo "  repo:    $REPO_ROOT"
echo "  profile: $PROFILE"
echo "  region:  $REGION"
echo

# 1. venv check
if [[ ! -x "$VENV_PY" ]]; then
  red "✗ venv not found at $VENV_PY"
  echo "  run: python -m venv .venv && .venv/bin/python -m pip install -e ."
  exit 1
fi
green "✓ venv ok"

# 2. AWS SSO
if ! aws sts get-caller-identity --profile "$PROFILE" >/dev/null 2>&1; then
  yellow "⟳ SSO session expired — running aws sso login --profile $PROFILE"
  aws sso login --profile "$PROFILE"
fi
green "✓ aws sts ok"

# 3. boto3 credential resolution from the venv
if ! AWS_PROFILE="$PROFILE" "$VENV_PY" -c "
import boto3
boto3.Session(profile_name='$PROFILE', region_name='$REGION').client('sts').get_caller_identity()
" >/dev/null 2>&1; then
  red "✗ venv boto3 cannot resolve credentials"
  echo "  try: $VENV_PY -m pip install --upgrade boto3 botocore"
  exit 1
fi
green "✓ venv boto3 ok"

# 4. restore fixture state (undo any prior visitor's edits) — skipped with --keep-state
cd "$REPO_ROOT"
if [[ $KEEP_STATE -eq 1 ]]; then
  green "✓ fixture preserved (--keep-state)"
elif ! git diff --quiet demo_recipe_api/recipe_api/ 2>/dev/null; then
  yellow "⟳ restoring demo_recipe_api/recipe_api/ to clean state"
  git restore demo_recipe_api/recipe_api/
  green "✓ fixture clean"
else
  green "✓ fixture clean"
fi

# 5. fresh trust.db (cold start) — skipped with --keep-state
TRUST_DB="$DEMO_DIR/.sc/trust.db"
if [[ $KEEP_STATE -eq 1 ]]; then
  green "✓ trust.db preserved (--keep-state)"
elif [[ -f "$TRUST_DB" ]]; then
  yellow "⟳ removing $TRUST_DB"
  rm -f "$TRUST_DB"
  green "✓ trust.db cleared (cold start)"
else
  green "✓ trust.db absent (cold start)"
fi

# 6. demo fixture tests — quick sanity
cd "$REPO_ROOT"
if ! PYTHONPATH=demo_recipe_api "$VENV_PY" -m pytest demo_recipe_api/tests -q >/dev/null 2>&1; then
  red "✗ demo_recipe_api tests failed"
  PYTHONPATH=demo_recipe_api "$VENV_PY" -m pytest demo_recipe_api/tests -q || true
  exit 1
fi
green "✓ demo tests pass"

# 7. port 5001 free
if lsof -ti tcp:5001 >/dev/null 2>&1; then
  yellow "⟳ port 5001 in use — killing"
  lsof -ti tcp:5001 | xargs kill -9 2>/dev/null || true
fi
green "✓ port 5001 free"

echo
cyan "→ ready to demo"
echo
echo "Terminal 1 (recipe app):"
echo "  cd $DEMO_DIR && $VENV_PY server.py"
echo "  → open http://localhost:5001"
echo
echo "Terminal 2 (Hedwig):"
echo "  export AWS_PROFILE=$PROFILE"
echo "  cd $DEMO_DIR && hw"
echo "  hedwig> /doctor       # verifies STS + Bedrock + noticer"
echo
echo "Between visitors:"
echo "  Just let the next visitor sit down — code and learned state persist on purpose."
echo "  Use 'hedwig> /reset-demo' only at end-of-day or if state is corrupted."
echo "  Use 'git restore demo_recipe_api/recipe_api/' only if a visitor's edit broke the app."
