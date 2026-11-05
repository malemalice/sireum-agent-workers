# Sireum worker installer for Windows 10/11.
# DRAFT — not tested yet. Read before running.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1 <join-code>
#
# Creates a WSL2 Ubuntu distro named "sireum" sized to half the PC, enables
# systemd, keeps it running, runs install-env.sh inside it, and adds a `sireum`
# command for Windows.
param([Parameter(Mandatory = $true)][string]$JoinCode)
$ErrorActionPreference = "Stop"
$Distro  = "sireum"

function Say($m)  { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Fail($m) { Write-Host "`nERROR: $m" -ForegroundColor Red; exit 1 }

if (-not $JoinCode.StartsWith("sireum1.")) { Fail "That doesn't look like a Sireum join code (it starts with 'sireum1.')" }

# 1. WSL itself
Say "Checking WSL"
wsl --status *> $null
if ($LASTEXITCODE -ne 0) {
  Say "Installing WSL (Windows may ask to restart; run this script again afterwards)"
  wsl --install --no-distribution
  Fail "Restart Windows, then run this script again."
}

# 2. Size: half the PC, at least 2 CPU / 4 GB
$cpus  = [math]::Max(2, [int]([Environment]::ProcessorCount / 2))
$memGb = [math]::Max(4, [int]((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB / 2))
Say "Configuring WSL ($cpus CPU, $memGb GB RAM)"
$wslconfig = Join-Path $env:USERPROFILE ".wslconfig"
@"
[wsl2]
processors=$cpus
memory=${memGb}GB
vmIdleTimeout=-1
"@ | Set-Content -Path $wslconfig -Encoding ascii
# vmIdleTimeout keeps the VM up; verify on your WSL version.

# 3. Ubuntu distro named "sireum"
$existing = (wsl -l -q) -replace "`0", "" | Where-Object { $_ -eq $Distro }
if (-not $existing) {
  Say "Installing Ubuntu as '$Distro'"
  wsl --install -d Ubuntu-24.04 --name $Distro --no-launch
  # On older WSL without --name: install Ubuntu-24.04, then export/import it as "sireum".
}

# 4. systemd on
Say "Enabling systemd inside the distro"
wsl -d $Distro -u root -- bash -c "printf '[boot]\nsystemd=true\n' > /etc/wsl.conf"
wsl --terminate $Distro

# 5. Install Sireum inside the distro
Say "Installing Sireum inside WSL"
# Copy this checkout into the distro (no download needed).
$RepoWin = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RepoWsl = (wsl -d $Distro -u root -- wslpath -a "$RepoWin").Trim()
wsl -d $Distro -u root -- bash -lc "rm -rf /tmp/sireum && mkdir -p /tmp/sireum && cp -a '$RepoWsl/.' /tmp/sireum/ && rm -rf /tmp/sireum/.git && SIREUM_HOST_OS=windows bash /tmp/sireum/install/install-env.sh '$JoinCode'"
if ($LASTEXITCODE -ne 0) { Fail "Install inside WSL failed; see the messages above." }

# 6. Keep it running after logon
Say "Starting Sireum automatically when you log in"
$action  = New-ScheduledTaskAction -Execute "wsl.exe" -Argument "-d $Distro --exec /bin/sleep infinity"
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "Sireum worker" -Action $action -Trigger $trigger -Force | Out-Null
Start-ScheduledTask -TaskName "Sireum worker"

# 7. `sireum` command for Windows
$bin = Join-Path $env:LOCALAPPDATA "sireum"
New-Item -ItemType Directory -Force -Path $bin | Out-Null
"@wsl -d $Distro -u root -- sireum %*" | Set-Content -Path (Join-Path $bin "sireum.cmd") -Encoding ascii
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$bin*") { [Environment]::SetEnvironmentVariable("Path", "$userPath;$bin", "User") }

Say "Seat tokens"
Write-Host "Sireum needs a Claude token for this PC (it uses your Claude Max plan)."
Write-Host "Run 'claude setup-token' on a machine with a browser, then register it with:  sireum seats"

Say "Done. Open a new terminal and check with:  sireum status"
Write-Host "Keep this PC logged in and awake while it's a worker."
