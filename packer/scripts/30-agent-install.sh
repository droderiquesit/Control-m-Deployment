#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# ctm-agent image ONLY (packer/ctm-agent.pkr.hcl — never part of the base
# ctm-rhel9 build): silent-install the Control-M/Agent from the build-time
# media staged in $CTM_AGENT_MEDIA_DIR (file-provisioned; DRKAI*/Agent* EPD
# archive + silent-agent*.xml from the control-m-agent / control-m-silent-xml
# packages).
#
# The install is deliberately UNCONFIGURED and INERT:
#   * the silent-agent.xml placeholders are filled with BUILD-TIME PLACEHOLDER
#     server settings (no real Control-M/Server exists at build time) and
#     START_AGENT is forced to "no" — the agent is NEVER started and NEVER
#     registered here. node-scripts/ctm_node_install.py configures the real
#     server name/ports/hostname, starts and registers it at first boot.
#   * no secrets exist in the agent XML or anywhere in this build.
# The media and the rendered XML are deleted before the image is captured.
# ---------------------------------------------------------------------------
set -euo pipefail

MEDIA_DIR="${CTM_AGENT_MEDIA_DIR:-/tmp/ctm-agent-media}"
AGENT_USER=ctmagent
AGENT_HOME="/home/${AGENT_USER}"
INSTALL_DIR="${AGENT_HOME}/ctm"

# Build-time placeholder server configuration (silent-agent.xml contract;
# see scripts/silent-install/README.md). ctm_node_install.py overwrites the
# server host/name/ports in CONFIG.dat on every instance's first boot.
PLACEHOLDER_SERVER_NAME=CTM_PLACEHOLDER
PLACEHOLDER_SERVER_HOST=ctm-srv-placeholder-a
AGENT_TO_SERVER_PORT=7005
SERVER_TO_AGENT_PORT=7006

[ -d "$MEDIA_DIR" ] || { echo "ERROR: media dir $MEDIA_DIR not found (file provisioner)"; exit 1; }

# 1. Create the agent OS user (same account the VMSS instances use).
id -u "$AGENT_USER" >/dev/null 2>&1 || useradd -m -d "$AGENT_HOME" -s /bin/bash "$AGENT_USER"
mkdir -p "$INSTALL_DIR"

# 2. Locate and extract the Agent EPD archive.
ARCHIVE=$(find "$MEDIA_DIR" -maxdepth 2 -type f \( -name 'DRKAI*' -o -name 'Agent*' \) | sort | head -n1)
[ -n "$ARCHIVE" ] || { echo "ERROR: no DRKAI*/Agent* EPD archive under $MEDIA_DIR"; exit 1; }
EXTRACT_DIR=/tmp/ctm-agent-extract
mkdir -p "$EXTRACT_DIR"
echo "== extracting $ARCHIVE"
tar -xf "$ARCHIVE" -C "$EXTRACT_DIR"

SETUP=$(find "$EXTRACT_DIR" -maxdepth 2 -type f -name setup.sh | sort | head -n1)
[ -n "$SETUP" ] || { echo "ERROR: setup.sh not found in the extracted agent media"; exit 1; }

# 3. Render silent-agent.xml with the placeholder contract. The XML ships in
#    the media dir (control-m-silent-xml package / scripts/silent-install/).
XML_SRC=$(find "$MEDIA_DIR" -maxdepth 2 -type f -name 'silent-agent*.xml' | sort | head -n1)
[ -n "$XML_SRC" ] || { echo "ERROR: no silent-agent*.xml under $MEDIA_DIR"; exit 1; }
XML=/tmp/ctm-silent-agent.xml
sed -e "s|__AGENT_HOME__|$INSTALL_DIR|g" \
    -e "s|__AGENT_USER__|$AGENT_USER|g" \
    -e "s|__HOSTNAME__|$(hostname -s)|g" \
    -e "s|__CTM_SERVER_NAME__|$PLACEHOLDER_SERVER_NAME|g" \
    -e "s|__PRIMARY_HOST__|$PLACEHOLDER_SERVER_HOST|g" \
    -e "s|__AGENT_TO_SERVER_PORT__|$AGENT_TO_SERVER_PORT|g" \
    -e "s|__SERVER_TO_AGENT_PORT__|$SERVER_TO_AGENT_PORT|g" \
    "$XML_SRC" > "$XML"
# DO NOT START at build time (the example XML ships START_AGENT=yes for the
# old boot-time install; the image must capture a stopped, inert agent).
sed -i 's|\(name="START_AGENT"[[:space:]]*value="\)[^"]*"|\1no"|' "$XML"
if grep -q '__[A-Z0-9_]*__' "$XML"; then
  echo "ERROR: unreplaced placeholders remain in $XML:"; grep -o '__[A-Z0-9_]*__' "$XML" | sort -u
  exit 1
fi
chown "$AGENT_USER:$AGENT_USER" "$XML"
chmod 0644 "$XML"

# 4. Silent install as the agent user. Never started, never registered.
chown -R "$AGENT_USER:$AGENT_USER" "$EXTRACT_DIR" "$INSTALL_DIR" "$AGENT_HOME"
echo "== installing Control-M/Agent (silent, unconfigured) from $SETUP"
su - "$AGENT_USER" -c "$SETUP -silent $XML"

# Belt-and-braces: if the installer started anything anyway, shut it down so
# the captured image holds no running (or boot-starting) agent processes.
su - "$AGENT_USER" -c "shut-ag -u $AGENT_USER -p ALL" >/dev/null 2>&1 || true

# 5. Clean the media, the extraction tree and the rendered XML — the image
#    carries the INSTALLED agent only, never installer media.
rm -rf "$MEDIA_DIR" "$EXTRACT_DIR" "$XML"

echo "== 30-agent-install: done (agent baked, unconfigured, not started)"
