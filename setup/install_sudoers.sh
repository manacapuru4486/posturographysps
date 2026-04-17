#!/bin/bash
# install_sudoers.sh – Allow the PosturoSPS server user to shut down the PC
# without being prompted for a password.
#
# Run ONCE as a user who can sudo:
#   chmod +x setup/install_sudoers.sh
#   sudo bash setup/install_sudoers.sh
#
# What it does:
#   Adds /etc/sudoers.d/posturosps-shutdown with a NOPASSWD rule for
#   "shutdown" and "systemctl poweroff" for the current (or specified) user.

set -e

TARGET_USER="${1:-$(logname 2>/dev/null || echo "$SUDO_USER")}"

if [ -z "$TARGET_USER" ]; then
    echo "Usage: sudo bash install_sudoers.sh <username>"
    echo "Example: sudo bash install_sudoers.sh utilisateur"
    exit 1
fi

SUDOERS_FILE="/etc/sudoers.d/posturosps-shutdown"

cat > "$SUDOERS_FILE" <<EOF
# PosturoSPS – allow server process to shut down the PC without password prompt
$TARGET_USER ALL=(ALL) NOPASSWD: /sbin/shutdown, /usr/sbin/shutdown, /bin/systemctl poweroff, /bin/systemctl halt
EOF

chmod 440 "$SUDOERS_FILE"

# Validate the file (visudo -c will catch syntax errors)
if visudo -c -f "$SUDOERS_FILE"; then
    echo "OK – sudoers rule created for user '$TARGET_USER'."
    echo "    File: $SUDOERS_FILE"
    echo "    The server can now shut down the PC via /system/shutdown."
else
    echo "ERROR – invalid sudoers syntax, removing file."
    rm -f "$SUDOERS_FILE"
    exit 1
fi
