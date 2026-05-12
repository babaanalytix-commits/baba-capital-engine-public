#!/bin/bash
# setup_and_push.sh — initialise the public repo + push to GitHub.
#
# Pre-requirement: you have a GitHub account, your CLI is authenticated
# (either via the `gh` CLI or via SSH keys / a personal access token),
# and you've decided the repo name on GitHub.
#
# Usage:
#   cd ~/Desktop/CoworkStation/baba-capital-engine-public
#   bash setup_and_push.sh
#
# The script will prompt for:
#   - Your GitHub username (e.g. babaanalytix-commits)
#   - Repo name (default: baba-capital-engine-public)
#   - Whether to create the GitHub repo via `gh` CLI or assume it already exists

set -euo pipefail

cd "$(dirname "$0")"

# ---------- 1. Pre-flight ----------
echo "============================================"
echo "  BABA Capital Engine — Public Repo Setup  "
echo "============================================"
echo

# Confirm we're in the right place
if [ ! -f README.md ] || [ ! -d strategies_catalogue ]; then
    echo "ERROR: Run this from the baba-capital-engine-public/ directory."
    exit 1
fi

# Confirm git is installed
if ! command -v git >/dev/null 2>&1; then
    echo "ERROR: git is not installed. Install it via Xcode CLI tools:"
    echo "  xcode-select --install"
    exit 1
fi

# ---------- 2. Gather inputs ----------
read -rp "GitHub username [babaanalytix-commits]: " GH_USER
GH_USER="${GH_USER:-babaanalytix-commits}"

read -rp "Repo name [baba-capital-engine-public]: " REPO_NAME
REPO_NAME="${REPO_NAME:-baba-capital-engine-public}"

read -rp "Create the GitHub repo now via gh CLI? [Y/n]: " CREATE_REPO
CREATE_REPO="${CREATE_REPO:-Y}"

# ---------- 3. git init ----------
if [ ! -d .git ]; then
    echo
    echo "→ Initialising git repository..."
    git init -b main
    git add .
    git commit -m "Initial public release: architecture, strategy catalogue, marker contract"
else
    echo "→ Git already initialised, skipping init."
    git add .
    git diff --cached --quiet || git commit -m "Update public release"
fi

# ---------- 4. Create remote ----------
REMOTE_URL="git@github.com:${GH_USER}/${REPO_NAME}.git"

if [[ "$CREATE_REPO" =~ ^[Yy] ]]; then
    if command -v gh >/dev/null 2>&1; then
        echo
        echo "→ Creating GitHub repo via gh CLI..."
        gh repo create "${GH_USER}/${REPO_NAME}" \
            --public \
            --source=. \
            --remote=origin \
            --description "BABA Capital Engine — public architecture, strategy taxonomy, and operational design for a multi-venue crypto trading system." \
            || echo "  (gh repo create errored — repo may already exist, continuing)"
    else
        echo
        echo "→ gh CLI not installed. Install with: brew install gh && gh auth login"
        echo "→ For now: create the repo manually at"
        echo "    https://github.com/new"
        echo "  Name it: ${REPO_NAME}"
        echo "  Description: BABA Capital Engine — public architecture..."
        echo "  Visibility: Public"
        echo "  Do NOT initialise with README, LICENSE, or .gitignore (we have them locally)"
        echo
        read -rp "Press Enter when the empty repo exists on GitHub..."
    fi
fi

# ---------- 5. Add remote + push ----------
if ! git remote get-url origin >/dev/null 2>&1; then
    git remote add origin "$REMOTE_URL"
fi

echo
echo "→ Pushing to ${REMOTE_URL}..."
git push -u origin main

# ---------- 6. Done ----------
echo
echo "============================================"
echo "  Done."
echo "============================================"
echo "  Repo: https://github.com/${GH_USER}/${REPO_NAME}"
echo
echo "  Next steps:"
echo "    1. Deploy contracts/BabaCapitalEngineMarker.sol on Base via Remix"
echo "       (see contracts/README.md — 60 seconds, ~\$0.01 of ETH)"
echo "    2. Update README.md with the deployed contract address"
echo "    3. Post on X: link the repo, tag @TalentProtocol and @base"
echo "    4. In Talent Protocol app: verify the repo is linked to your GitHub"
echo
