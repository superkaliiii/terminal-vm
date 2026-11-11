# terminal-vm installer for Windows: the tool, what it needs (Python, QEMU, the Windows Hypervisor Platform) after one
# confirmation, the command on the PATH, and Claude Code's MCP server and skill when Claude Code is there.
#   irm https://raw.githubusercontent.com/superkaliiii/terminal-vm/main/terminal-vm/install.ps1 | iex
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-Yes] [-NoDeps] [-NoMcp] [-Update] [-Uninstall]
# Through irm | iex options cannot be passed: set TERMINAL_VM_INSTALL="-Yes -NoMcp" (for example) first.
param([switch]$Yes, [switch]$NoDeps, [switch]$NoMcp, [switch]$Update, [switch]$Uninstall)
$Tool = "terminal-vm"
$File = "terminal_vm.py"
$Skill = "skills\terminal-vm\SKILL.md"
$Archive = if ($env:TERMINAL_VM_ARCHIVE) { $env:TERMINAL_VM_ARCHIVE } else { "https://github.com/superkaliiii/terminal-vm/archive/refs/heads/main.zip" }
$SubDir = "terminal-vm"
$PyMin = 8
$WinPython = $true  # it runs on Windows itself
$Opts = $env:TERMINAL_VM_INSTALL

function Has-Qemu { return [bool]((Get-Command qemu-system-x86_64 -ErrorAction SilentlyContinue) -or (Test-Path "$env:ProgramFiles\qemu\qemu-system-x86_64.exe")) }
function Has-Whpx {
    try { return (Get-CimInstance Win32_OptionalFeature -Filter "Name='HypervisorPlatform'").InstallState -eq 1 } catch { return $true }
}
function Deps-Missing {  # what this tool needs that is not here, in words for the one question
    $m = @()
    if (-not (Has-Qemu)) { $m += "QEMU (winget)" }
    if (-not (Has-Whpx)) { $m += "the Windows Hypervisor Platform (administrator; Windows restarts later)" }
    return $m
}
function Install-Deps {
    if (-not (Has-Qemu)) { Winget-Install "SoftwareFreedomConservancy.QEMU" }
    if (-not (Has-Whpx)) {
        if (Run-Elevated "-NoProfile -Command Enable-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform -NoRestart") {
            $script:Restart = $true
        }
    }
}

function Install-Tool($src) {
    New-Item -ItemType Directory -Force (Join-Path $Lib (Split-Path $Skill)) | Out-Null
    Copy-Item (Join-Path $src $File) (Join-Path $Lib $File) -Force
    Copy-Item (Join-Path $src $Skill) (Join-Path $Lib $Skill) -Force
    # the py launcher survives a Python upgrade; else this Python; in the console's code page (a non-ASCII path)
    $py = if (Get-Command py -ErrorAction SilentlyContinue) { "py -3" } else { "`"$Python`"" }
    Write-Oem (Join-Path $Lib "$Tool.cmd") "@$py `"%~dp0$File`" %*"
    return (Join-Path $Lib "$Tool.cmd")
}

# a .cmd is not started by MCP clients (Node spawns no batch file without a shell): Python itself
function Mcp-Command { return @($Python, (Join-Path $Lib $File), "mcp") }

function Install-Skill($src) { & $Cmd skills install 2>$null | Out-Null; return ($LASTEXITCODE -eq 0) }

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
function Write-Oem($path, $text) {  # what cmd.exe reads a .cmd in: the console's OEM code page
    $oem = [Text.Encoding]::GetEncoding([Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage)
    [IO.File]::WriteAllText($path, $text + "`r`n", $oem)
}
function Run-Elevated($arguments, $exe = "powershell") {  # one UAC prompt; false when refused or failed
    try { $p = Start-Process $exe -Verb RunAs -Wait -PassThru -ArgumentList $arguments } catch { Say "not done: administrator rights refused"; return $false }
    if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) { Say "not done: $exe exited with $($p.ExitCode)"; return $false }
    return $true  # 3010: done, a restart finishes it
}
function Winget-Install($id) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { Say "winget not found: install $id yourself"; return }
    & winget install --id $id -e --silent --accept-source-agreements --accept-package-agreements | Out-Host
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
function Find-Python {
    foreach ($c in @(@("py", "-3"), @("python"), @("python3"))) {
        if (-not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { continue }
        $pre = @($c | Select-Object -Skip 1)
        $exe = & $c[0] @pre -c "import sys; print(sys.executable if sys.version_info >= (3, $PyMin) else '')" 2>$null
        if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path $exe)) { return $exe }  # not the Store's empty alias
    }
    return $null
}
function User-Path($add, $remove) {
    # the user's PATH as stored (REG_EXPAND_SZ: %USERPROFILE%\... stays a variable), then a broadcast for new windows
    $key = Get-Item "HKCU:\Environment"
    $raw = [string]$key.GetValue("Path", "", "DoNotExpandEnvironmentNames")
    $parts = @(($raw -split ";") | Where-Object { $_ -and $_ -ne $remove -and $_ -ne $add })
    if ($add) { $parts += $add }
    $kind = if ($raw -match "%" -or -not $raw) { "ExpandString" } else { $key.GetValueKind("Path") }  # as it was
    Set-ItemProperty "HKCU:\Environment" -Name Path -Value ($parts -join ";") -Type $kind
    [Environment]::SetEnvironmentVariable("TERMINAL_INSTALL_REFRESH", "1", "User")  # sends WM_SETTINGCHANGE
    [Environment]::SetEnvironmentVariable("TERMINAL_INSTALL_REFRESH", $null, "User")
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

# 1. what it needs: one question for all of it
$Python = if ($WinPython) { Find-Python } else { $null }
if (-not $NoDeps) {
    $missing = @(Deps-Missing)
    if ($WinPython -and -not $Python) { $missing = @("Python 3.$PyMin+ (winget)") + $missing }
    if ($missing.Count -and (Ask "$Tool needs: $($missing -join ', '). Install now?")) {
        if ($WinPython -and -not $Python) { Winget-Install "Python.Python.3.12"; $Python = Find-Python }
        Install-Deps
    } elseif ($missing.Count) { Say "not installed: $($missing -join ', ') ($Tool doctor says how)" }
}
if ($WinPython -and -not $Python) { throw "Python 3.$PyMin or later is required (winget install Python.Python.3.12)" }

# 2. the tool: from this folder when it holds it, else downloaded
$src = if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot $File))) { $PSScriptRoot } else { $null }
$tmp = $null
if (-not $src) {
    $tmp = Join-Path ([IO.Path]::GetTempPath()) "$Tool-$PID"
    New-Item -ItemType Directory -Force $tmp | Out-Null
    Say "downloading $Tool..."
    Invoke-WebRequest -UseBasicParsing $Archive -OutFile "$tmp\a.zip"
    Expand-Archive "$tmp\a.zip" $tmp -Force
    $found = Get-ChildItem -Recurse -Path $tmp -Filter $File | Where-Object { (Split-Path $_.DirectoryName -Leaf) -eq $SubDir -or -not $SubDir } | Select-Object -First 1
    if (-not $found) { Remove-Item -Recurse -Force $tmp; throw "the download holds no $File" }
    $src = $found.DirectoryName
}
try {
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
    $Mcp = Mcp-Command
    if (-not $NoMcp -and (Get-Command claude -ErrorAction SilentlyContinue)) {
        & claude mcp remove $Tool -s user 2>$null | Out-Null  # an update: the path may have changed
        & claude mcp add -s user $Tool -- @Mcp | Out-Null
        if ($LASTEXITCODE -eq 0) { Say "MCP server $Tool declared in Claude Code" }
        else { Say "could not declare the MCP server: claude mcp add -s user $Tool -- $($Mcp -join ' ')" }
        if (Install-Skill $src) { Say "skill installed in ~\.claude\skills\$Tool" }
    } else {
        Say "for an agent: MCP server = $($Mcp -join ' ')"
    }
} finally {
    if ($tmp) { Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue }
}

# 5. what is left to fix, if anything
Say ""
& $Cmd doctor
if ($LASTEXITCODE -ne 0) { Say "doctor found something to fix: each line above gives the command" }
if ($script:Restart) { Say "restart Windows to finish (the Windows Hypervisor Platform, or WSL)" }
