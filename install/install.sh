#!/usr/bin/env bash
# Sireum worker installer for macOS and Linux.
# DRAFT — not tested yet. Read before running.
#
#   ./install.sh <join-code>
#
# macOS: creates a Lima Ubuntu VM named "sireum" sized to half the Mac, then runs
#        install-env.sh inside it, and installs a `sireum` helper on the Mac.
# Linux: runs install-env.sh directly on this machine.
set -euo pipefail

JOIN_CODE="${1:-}"
[[ -f "$(cd "$(dirname "$0")" && pwd)/install-env.sh" ]] || { echo "Run this from a Sireum checkout (install/install.sh)" >&2; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[[ -n "$JOIN_CODE" ]] || fail "Usage: ./install.sh <join-code>   (ask the operator for a join code)"
[[ "$JOIN_CODE" == sireum1.* ]] || fail "That doesn't look like a Sireum join code (it starts with 'sireum1.')"

case "$(uname -s)" in
  Darwin)
    say "macOS detected"
    command -v brew >/dev/null || fail "Homebrew is required: https://brew.sh — install it, then run this again"
    command -v limactl >/dev/null || { say "Installing Lima"; brew install lima; }

    # Half the Mac's CPU and RAM, at least 2 CPU / 4 GB.
    CPUS=$(( $(sysctl -n hw.ncpu) / 2 )); (( CPUS < 2 )) && CPUS=2
    MEM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 / 2 )); (( MEM_GB < 4 )) && MEM_GB=4
    say "Creating the Sireum Linux VM (${CPUS} CPU, ${MEM_GB} GB RAM, 80 GB disk)"
    if limactl list -q 2>/dev/null | grep -qx sireum; then
      echo "VM 'sireum' already exists; starting it"
      limactl start sireum
    else
      # Verify the template name on your Lima version (template:ubuntu-lts on Lima 2.x).
      limactl start --name=sireum --vm-type=vz --cpus="$CPUS" --memory="$MEM_GB" --disk=80 \
        --tty=false template://ubuntu-lts
    fi

    say "Setting the VM to start when you log in"
    PLIST="$HOME/Library/LaunchAgents/id.sireum.vm.plist"
    mkdir -p "$(dirname "$PLIST")"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>id.sireum.vm</string>
  <key>ProgramArguments</key><array><string>$(command -v limactl)</string><string>start</string><string>sireum</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
EOF
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"

    say "Installing Sireum inside the VM"
    # Copy this checkout into the VM (the repo may sit in a protected folder like ~/Documents).
    tar -C "$HERE/.." --exclude .git --exclude __pycache__ -czf - . \
      | limactl shell --workdir / sireum -- bash -c 'rm -rf /tmp/sireum && mkdir -p /tmp/sireum && tar -xzf - -C /tmp/sireum'
    limactl shell --workdir / sireum -- sudo SIREUM_HOST_OS=macos bash /tmp/sireum/install/install-env.sh "$JOIN_CODE"

    say "Installing the 'sireum' command on this Mac"
    # BSD install(1) on macOS can't read a pipe; stage via a file.
    tmp=$(mktemp)
    cat > "$tmp" <<'EOF'
#!/usr/bin/env bash
# Forwards to the sireum helper inside the Lima VM.
exec limactl shell --workdir / sireum -- sudo sireum "$@"
EOF
    sudo install -d -m 0755 /usr/local/bin
    sudo install -m 0755 "$tmp" /usr/local/bin/sireum
    rm -f "$tmp"

    say "Seat tokens"
    echo "Sireum needs a Claude token for this PC (it uses your Claude Max plan)."
    echo "A browser window will open. Sign in, then paste the token when asked."
    if command -v claude >/dev/null; then
      claude setup-token || true
    else
      echo "Claude Code isn't installed on this Mac. Install it (npm i -g @anthropic-ai/claude-code), then run: claude setup-token"
    fi
    echo
    echo "Then register the token(s) with:  sireum seats"
    ;;

  Linux)
    say "Linux detected — installing directly on this machine"
    sudo SIREUM_HOST_OS=linux bash "$HERE/install-env.sh" "$JOIN_CODE"
    echo
    echo "Next: create a Claude token on a machine with a browser (claude setup-token),"
    echo "then register it here with:  sudo sireum seats"
    ;;

  *)
    fail "Unsupported OS. On Windows, use install.ps1 instead."
    ;;
esac

say "Done. Check with:  sireum status"
