# terminal-vm installer for Windows: the tool, what it needs (Python, QEMU, the Windows Hypervisor Platform) after one
# confirmation, the command on the PATH, and Claude Code's MCP server and skill when Claude Code is there.
#   irm https://raw.githubusercontent.com/superkaliiii/terminal-mac-inspection/unified/terminal-vm/install.ps1 | iex
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-Yes] [-NoDeps] [-NoMcp] [-Update] [-Uninstall]
# Through irm | iex options cannot be passed: set TERMINAL_VM_INSTALL="-Yes -NoMcp" (for example) first.
param([switch]$Yes, [switch]$NoDeps, [switch]$NoMcp, [switch]$Update, [switch]$Uninstall)
$Tool = "terminal-vm"
$File = "terminal_vm.py"
$Skill = "skills\terminal-vm\SKILL.md"
$Archive = if ($env:TERMINAL_VM_ARCHIVE) { $env:TERMINAL_VM_ARCHIVE } else { "https://github.com/superkaliiii/terminal-mac-inspection/archive/refs/heads/unified.zip" }
$SubDir = "terminal-vm"
$PyMin = 8
$WinPython = $true  # it runs on Windows itself
$Opts = $env:TERMINAL_VM_INSTALL

function Install-Deps {
    # QEMU, then the Windows Hypervisor Platform (without it VMs run ten times slower)
    $qemu = (Get-Command qemu-system-x86_64 -ErrorAction SilentlyContinue) -or (Test-Path "$env:ProgramFiles\qemu\qemu-system-x86_64.exe")
    if (-not $qemu) {
        if (Ask "terminal-vm needs QEMU. Install it now (winget)?") { Winget-Install "SoftwareFreedomConservancy.QEMU" }
        else { Say "not installed: QEMU (terminal-vm doctor says how)" }
    }
    $whpx = ""
    try { $whpx = (Get-CimInstance Win32_OptionalFeature -Filter "Name='HypervisorPlatform'").InstallState } catch { }
    if ($whpx -ne 1) {
        if (Ask "Turn on the Windows Hypervisor Platform (administrator; Windows restarts later to finish)?") {
            Start-Process powershell -Verb RunAs -Wait -ArgumentList "-NoProfile -Command Enable-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform -NoRestart"
            $script:Restart = $true
        }
    }
}

function Install-Tool($src) {
    New-Item -ItemType Directory -Force (Join-Path $Lib (Split-Path $Skill)) | Out-Null
    Copy-Item (Join-Path $src $File) (Join-Path $Lib $File) -Force
    Copy-Item (Join-Path $src $Skill) (Join-Path $Lib $Skill) -Force
    Set-Content -Encoding ascii (Join-Path $Lib "$Tool.cmd") "@`"$Python`" `"%~dp0$File`" %*"
    return (Join-Path $Lib "$Tool.cmd")
}

# ===== common to terminal-ct and terminal-vm's install.ps1 =====
if ($Opts) {
    foreach ($o in $Opts.Split(" ", [StringSplitOptions]::RemoveEmptyEntries)) {
        switch ($o) { "-Yes" { $Yes = $true } "-NoDeps" { $NoDeps = $true } "-NoMcp" { $NoMcp = $true }
                      "-Update" { $Update = $true } "-Uninstall" { $Uninstall = $true } default { throw "unknown option $o" } }
    }
}
$Lib = Join-Path $env:LOCALAPPDATA "Programs\$Tool"  # the program; its data (VMs, containers) is elsewhere, kept
$script:Restart = $false
function Say($m) { Write-Host $m }
function Ask($q) {
    if ($Yes) { return $true }
    if (-not [Environment]::UserInteractive) { Say "$q [skipped: no one to ask; run again with -Yes]"; return $false }
    $a = Read-Host "$q [Y/n]"
    return ($a -eq "" -or $a -match "^(y|yes|o|oui)$")
}
function Winget-Install($id) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { Say "winget not found: install $id yourself"; return }
    & winget install --id $id -e --silent --accept-source-agreements --accept-package-agreements | Out-Host
    # what it put on the PATH, for this session too
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
function Find-Python {
    foreach ($c in @(@("py", "-3"), @("python"), @("python3"))) {
        if (-not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { continue }
        $exe = & $c[0] @($c[1..9] | Where-Object { $_ }) -c "import sys; print(sys.executable if sys.version_info >= (3, $PyMin) else '')" 2>$null
        if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path $exe)) { return $exe }  # not the Store's empty alias
    }
    return $null
}
function User-Path($add, $remove) {
    $p = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @(($p -split ";") | Where-Object { $_ -and $_ -ne $remove -and $_ -ne $add })
    if ($add) { $parts += $add }
    [Environment]::SetEnvironmentVariable("Path", ($parts -join ";"), "User")
}

if ($Uninstall) {
    if (-not $NoMcp -and (Get-Command claude -ErrorAction SilentlyContinue)) {
        & claude mcp remove $Tool -s user 2>$null | Out-Null
        Remove-Item -Recurse -Force (Join-Path $HOME ".claude\skills\$Tool") -ErrorAction SilentlyContinue
    }
    if (Get-Command Uninstall-Extra -ErrorAction SilentlyContinue) { Uninstall-Extra }
    Remove-Item -Recurse -Force $Lib -ErrorAction SilentlyContinue
    User-Path $null $Lib
    Say "$Tool removed. What it made stays (remove it first with $Tool itself if you want it gone)."
    return
}

# 1. what it needs
$Python = if ($WinPython) { Find-Python } else { $null }
if (-not $NoDeps) {
    if ($WinPython -and -not $Python -and (Ask "$Tool needs Python 3.$PyMin or later. Install it now (winget)?")) {
        Winget-Install "Python.Python.3.12"
        $Python = Find-Python
    }
    Install-Deps
}
if ($WinPython -and -not $Python) { throw "Python 3.$PyMin or later is required (winget install Python.Python.3.12)" }

# 2. the tool: from this folder when it holds it, else downloaded
$src = if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot $File))) { $PSScriptRoot } else { $null }
if (-not $src) {
    $tmp = Join-Path ([IO.Path]::GetTempPath()) "$Tool-$PID"
    New-Item -ItemType Directory -Force $tmp | Out-Null
    Say "downloading $Tool..."
    Invoke-WebRequest -UseBasicParsing $Archive -OutFile "$tmp\a.zip"
    Expand-Archive "$tmp\a.zip" $tmp -Force
    $found = Get-ChildItem -Recurse -Path $tmp -Filter $File | Where-Object { (Split-Path $_.DirectoryName -Leaf) -eq $SubDir -or -not $SubDir } | Select-Object -First 1
    if (-not $found) { throw "the download holds no $File" }
    $src = $found.DirectoryName
}
$Cmd = Install-Tool $src
if (-not $Cmd) { return }  # not finished (a restart first): said why
Say "installed: $Cmd"

# 3. on the PATH (the user's: no administrator)
if (-not (($env:Path -split ";") -contains (Split-Path $Cmd))) {
    User-Path (Split-Path $Cmd) $null
    $env:Path += ";" + (Split-Path $Cmd)
    Say "added $(Split-Path $Cmd) to your PATH: open a new terminal to use $Tool"
}

# 4. the agent: Claude Code's MCP server and skill
$Mcp = if (Get-Command Mcp-Command -ErrorAction SilentlyContinue) { Mcp-Command } else { @($Cmd, "mcp") }
if (-not $NoMcp -and (Get-Command claude -ErrorAction SilentlyContinue)) {
    & claude mcp remove $Tool -s user 2>$null | Out-Null  # an update: the path may have changed
    & claude mcp add -s user $Tool -- @Mcp | Out-Null
    if ($LASTEXITCODE -eq 0) { Say "MCP server $Tool declared in Claude Code" }
    else { Say "could not declare the MCP server: claude mcp add -s user $Tool -- $($Mcp -join ' ')" }
    & $Cmd skills install 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { Say "skill installed in ~\.claude\skills\$Tool" }
} else {
    Say "for an agent: MCP server = $($Mcp -join ' ')"
}

# 5. what is left to fix, if anything
Say ""
& $Cmd doctor
if ($LASTEXITCODE -ne 0) { Say "doctor found something to fix: each line above gives the command" }
if ($script:Restart) { Say "restart Windows to finish turning on the Windows Hypervisor Platform" }
