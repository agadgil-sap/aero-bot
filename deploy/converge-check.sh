#!/usr/bin/env bash
# Convergence check: prove the deployed /opt tree matches this checkout's HEAD.
#
# Compares the content-addressed inventory (sha256 of every tracked file) of a
# local `git archive HEAD` extraction against the remote application tree, so
# a stale DEPLOYED_COMMIT stamp can never masquerade as convergence.
#
# Excluded from both sides: .venv, __pycache__, *.pyc, .pytest_cache,
# .mypy_cache, .ruff_cache (build artifacts), and DEPLOYED_COMMIT (stamped
# per install, intentionally not part of the tree). Remote-only files whose
# name carries ".pre-" are the known rollback snapshots from the 2026-09-20
# on-box hotfix sessions (suffix form: <file>.pre-<tag>-<timestamp>); they
# are reported, not treated as divergence, and should be removed during the
# next deploy.
#
# Usage from the repository root (any platform; macOS uses shasum -a 256):
#   bash deploy/converge-check.sh
#
# Override the SSH transport or the remote app dir through the environment:
#   AERO_BOT_SSH='gcloud compute ssh aero-bot --zone us-west1-b --quiet'
#   AERO_BOT_REMOTE_APP=/opt/aero-bot bash deploy/converge-check.sh
#
# Exits 0 only when every compared file is byte-identical (snapshot extras
# aside); exits 1 on any divergence or read failure.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE_APP="${AERO_BOT_REMOTE_APP:-/opt/aero-bot}"
SSH_CMD="${AERO_BOT_SSH:-gcloud compute ssh aero-bot --zone us-west1-b --quiet}"

log() { printf '[converge-check] %s\n' "$*"; }
die() { printf '[converge-check] FATAL: %s\n' "$*" >&2; exit 1; }

command -v git >/dev/null 2>&1 || die "git is required"
[[ -d "${REPO_ROOT}/.git" ]] || die "run from a git checkout of the repository"

if command -v sha256sum >/dev/null 2>&1; then
    HASH=(sha256sum)
elif command -v shasum >/dev/null 2>&1; then
    HASH=(shasum -a 256)
else
    die "neither sha256sum nor shasum is available"
fi

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
EXTRACT="${WORK}/tree"
mkdir -p "${EXTRACT}"

# ---------------------------------------------------------------- local side
log "extracting git archive HEAD"
git -C "${REPO_ROOT}" archive --format=tar HEAD | tar -x -C "${EXTRACT}"
(
    cd "${EXTRACT}"
    find . -type f \
        ! -path './.venv/*' ! -path '*__pycache__*' ! -name '*.pyc' \
        ! -path '*.pytest_cache*' ! -path '*.mypy_cache*' ! -path '*.ruff_cache*' \
        ! -name DEPLOYED_COMMIT -print0 \
        | LC_ALL=C sort -z | xargs -0 "${HASH[@]}"
) >"${WORK}/local.txt"
log "local side: $(wc -l <"${WORK}/local.txt" | tr -d ' ') files hashed"

# ---------------------------------------------------------------- remote side
log "hashing the remote tree over SSH (first connect may take a moment)"
# The deployed tree is root-group 0750, so the whole sweep - cd included -
# runs elevated; the SSH user never needs traverse rights to /opt.
REMOTE_SCRIPT="sudo sh -c 'cd ${REMOTE_APP} && find . -type f \
! -path \"./.venv/*\" ! -path \"*__pycache__*\" ! -name \"*.pyc\" \
! -path \"*.pytest_cache*\" ! -path \"*.mypy_cache*\" ! -path \"*.ruff_cache*\" \
! -name DEPLOYED_COMMIT -print0 | LC_ALL=C sort -z | xargs -0 sha256sum'"
# shellcheck disable=SC2086
if ! ${SSH_CMD} --command="${REMOTE_SCRIPT}" >"${WORK}/remote.txt" 2>"${WORK}/ssh.err"; then
    cat "${WORK}/ssh.err" >&2
    die "the remote hash sweep failed"
fi
# gcloud banners and motd land on stdout ahead of the hashes; keep lines
# that look like hash output only.
grep -E '^[0-9a-f]{64}  ' "${WORK}/remote.txt" >"${WORK}/remote.hashes" || true
if [[ ! -s "${WORK}/remote.hashes" ]]; then
    cat "${WORK}/ssh.err" >&2 || true
    die "the remote side returned no hashes"
fi
log "remote side: $(wc -l <"${WORK}/remote.hashes" | tr -d ' ') files hashed"

# ---------------------------------------------------------------- verdict
if diff -u "${WORK}/local.txt" "${WORK}/remote.hashes" >"${WORK}/diff.txt"; then
    log "VERDICT: converged - local HEAD and ${REMOTE_APP} are byte-identical"
    exit 0
fi

snapshots=$(grep -c '^+[0-9a-f]*  .*\.pre-' "${WORK}/diff.txt" || true)
real_lines=$(grep -cE '^[+-][0-9a-f]{64}  ' "${WORK}/diff.txt" || true)
if [[ "${real_lines}" -eq $((snapshots)) ]]; then
    log "VERDICT: converged aside from ${snapshots} known .pre-* rollback snapshot files"
    log "remove the ${REMOTE_APP}/.pre-* snapshots during the next deploy"
    exit 0
fi

cat "${WORK}/diff.txt"
log "VERDICT: DIVERGED - ${real_lines} differing lines (see the diff above)"
log "deploy with deploy/install.sh from the converged checkout, then re-run this check"
exit 1
