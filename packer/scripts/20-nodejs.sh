#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Golden image: Node.js 20 + npm for the Control-M Automation API CLI.
# Agents need it to self-provision/register at boot; harmless on EM/Server
# nodes, which share the same image.
# ---------------------------------------------------------------------------
set -euo pipefail

dnf -y module enable nodejs:20
dnf -y install nodejs npm

node --version
npm --version

echo "== 20-nodejs: done"
