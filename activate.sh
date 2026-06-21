#!/usr/bin/env bash
# Dev environment bootstrap. SOURCE this (don't execute) so the venv activation
# and exports land in your current shell:
#
#     source ./activate.sh
#
# It activates the virtualenv and points GOOGLE_APPLICATION_CREDENTIALS at the
# repo's service-account key using an ABSOLUTE path derived from this script's
# own location — so it works no matter which directory you run it from.

# Resolve this script's path when sourced (bash uses BASH_SOURCE, zsh uses %N).
if [ -n "${BASH_SOURCE:-}" ]; then
  _self="${BASH_SOURCE[0]}"
else
  _self="${(%):-%N}"
fi
REPO_ROOT="$(cd "$(dirname "$_self")" && pwd)"

# 1) Activate the virtualenv.
if [ -f "$REPO_ROOT/venv/bin/activate" ]; then
  source "$REPO_ROOT/venv/bin/activate"
else
  echo "activate.sh: no venv found at $REPO_ROOT/venv — create it first (python -m venv venv)" >&2
fi

# 2) Point Google auth at the SA key (absolute → CWD-independent).
export GOOGLE_APPLICATION_CREDENTIALS="$REPO_ROOT/gcp/sa_key.json"
if [ ! -f "$GOOGLE_APPLICATION_CREDENTIALS" ]; then
  echo "activate.sh: warning — key file not found at $GOOGLE_APPLICATION_CREDENTIALS" >&2
fi

echo "dev env ready:"
echo "  venv:  $(command -v python)"
echo "  creds: $GOOGLE_APPLICATION_CREDENTIALS"
