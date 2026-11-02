# Installs (or upgrades) Pengumon on Windows and starts it when you sign in. Needs Windows PowerShell 5.1 or newer; no
# administrator rights.
#
#   irm https://github.com/pengupowered/pengumon/releases/latest/download/install.ps1 | iex
#   .\install.ps1 [-Version X.Y.Z] [-Port N] [-NoService] [-NoBrowser]
#   .\install.ps1 -From DIR       install from files on disk (DIR has pengumon-X.Y.Z-py3-none-any.whl,
#                                 pengumon-X.Y.Z.lock and SHA256SUMS), for CI and computers without internet
#   .\install.ps1 -Uninstall      remove the sign-in task and the program; your data stays
#
# What it does, in order:
#   1. gets the wheel, the lock (every dependency pinned with hashes) and SHA256SUMS from the GitHub release and
#      checks the wheel and the lock against SHA256SUMS;
#   2. downloads uv (version and sha256 pinned below, per platform) and checks it;
#   3. on an upgrade, runs `pengumon backup` and removes the old sign-in task;
#   4. installs Python 3.12 (managed by uv, kept inside the app folder) and a virtual environment, then the locked
#      dependencies with `uv pip install --require-hashes --only-binary :all:`, then the wheel;
#   5. runs `pengumon service install` (a Task Scheduler task that starts Pengumon, without a window, when you sign
#      in), waits until it answers, and opens the browser.
#
# The program goes to %LOCALAPPDATA%\Programs\Pengumon, your data to %LOCALAPPDATA%\Pengumon.
param(
    [string]$Version = $env:PENGUMON_VERSION,
    [string]$From = '',
    [int]$Port = 0,
    [switch]$NoService,
    [switch]$NoBrowser,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is very slow with its progress bar on Windows PowerShell 5.1
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }

$Repo = 'pengupowered/pengumon'
$UvVersion = '0.13.4'
# sha256 of the uv release archives (https://github.com/astral-sh/uv/releases/tag/0.13.4)
$UvSha256 = @{
    'x86_64-pc-windows-msvc'  = 'ccec7508a07d9933d7cb1261880633efcf1f30d6e6eddab21823f1dae6f23225'
    'aarch64-pc-windows-msvc' = 'c4bc5a9534b2bb6a12fe29ea748445e1a6f9cb7bb275211c4fb74c52ecf317a5'
}
$PythonVersion = '3.12'
$BaseUrl = $env:PENGUMON_RELEASE_URL

function Say([string]$Text) { Write-Host $Text }
function Fail([string]$Text) { throw "install.ps1: $Text" }

function Invoke-Native {
    # Runs a program and stops on a non-zero exit code (PowerShell does not, for native commands).
    param([string]$File, [string[]]$Arguments)
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) { Fail "$File $($Arguments -join ' ') failed with exit code $LASTEXITCODE" }
}

function Get-Sha256([string]$Path) { (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant() }

$App = Join-Path $env:LOCALAPPDATA 'Programs\Pengumon'
$Data = Join-Path $env:LOCALAPPDATA 'Pengumon'
$Venv = Join-Path $App 'venv'
$Pengumon = Join-Path $Venv 'Scripts\pengumon.exe'

if ($Uninstall) {
    if (Test-Path $Pengumon) {
        & $Pengumon service uninstall
        # the task is gone, so nothing holds the program open; its files can go
        for ($i = 0; $i -lt 5; $i++) {
            try { Remove-Item -Recurse -Force $App -ErrorAction Stop; break } catch { if ($i -eq 4) { throw }; Start-Sleep -Seconds 1 }
        }
        Say "Pengumon is removed. Your data is still in $Data (delete it yourself if you do not want it)."
    } else {
        Say "Nothing to remove: $Pengumon does not exist."
    }
    return
}

$Arch = $env:PROCESSOR_ARCHITECTURE
if ($env:PROCESSOR_ARCHITEW6432) { $Arch = $env:PROCESSOR_ARCHITEW6432 }
switch ($Arch) {
    'AMD64' { $UvTarget = 'x86_64-pc-windows-msvc' }
    'ARM64' { $UvTarget = 'aarch64-pc-windows-msvc' }
    default { Fail "unsupported processor: $Arch (Pengumon needs 64-bit Windows)" }
}

$Work = Join-Path ([IO.Path]::GetTempPath()) ("pengumon-install-" + [Guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $Work | Out-Null
try {
    # ---- the release -------------------------------------------------------------------------------------------------
    if ($From -match '[\r\n]') { Fail '-From must not contain a newline' }
    if ($From) {
        if (-not (Test-Path -LiteralPath $From -PathType Container)) { Fail "-From: $From is not a folder" }
        if (-not $Version) {
            $wheels = @(Get-ChildItem -LiteralPath $From -Filter 'pengumon-*-py3-none-any.whl')
            if ($wheels.Count -ne 1) { Fail "-From: expected exactly one pengumon-*-py3-none-any.whl in $From" }
            $Version = ($wheels[0].Name -replace '^pengumon-([0-9][^-]*)-.*$', '$1')
        }
    } else {
        if (-not $Version) {
            Say 'Looking up the latest release...'
            $Version = (Invoke-RestMethod -UseBasicParsing "https://api.github.com/repos/$Repo/releases/latest").tag_name -replace '^v', ''
        }
        if (-not $BaseUrl) { $BaseUrl = "https://github.com/$Repo/releases/download/v$Version" }
    }
    $Version = $Version -replace '^v', ''
    if ($Version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$') { Fail "not a version number: $Version" }
    $Wheel = "pengumon-$Version-py3-none-any.whl"
    $Lock = "pengumon-$Version.lock"
    Say "Pengumon $Version"

    foreach ($name in @($Wheel, $Lock, 'SHA256SUMS')) {
        if ($From) {
            $source = Join-Path $From $name
            if (-not (Test-Path -LiteralPath $source)) { Fail "-From: $source is missing" }
            Copy-Item -LiteralPath $source -Destination (Join-Path $Work $name)
        } else {
            Invoke-WebRequest -UseBasicParsing -Uri "$BaseUrl/$name" -OutFile (Join-Path $Work $name)
        }
    }

    function Confirm-Sha256([string]$Name) {
        $want = $null
        foreach ($line in Get-Content -LiteralPath (Join-Path $Work 'SHA256SUMS')) {
            if ($line -match '^([0-9a-fA-F]{64}) [ *](.+)$' -and $Matches[2] -eq $Name) { $want = $Matches[1].ToLowerInvariant(); break }
        }
        if (-not $want) { Fail "SHA256SUMS does not list $Name" }
        $have = Get-Sha256 (Join-Path $Work $Name)
        if ($have -ne $want) { Fail "$Name does not match SHA256SUMS (expected $want, got $have); nothing was installed" }
    }
    Confirm-Sha256 $Wheel
    Confirm-Sha256 $Lock
    Say "Checked $Wheel and $Lock against SHA256SUMS."

    # ---- uv ----------------------------------------------------------------------------------------------------------
    $UvArchive = Join-Path $Work "uv-$UvTarget.zip"
    Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-$UvTarget.zip" -OutFile $UvArchive
    if ((Get-Sha256 $UvArchive) -ne $UvSha256[$UvTarget]) { Fail "uv-$UvTarget.zip does not match the sha256 pinned in this script; nothing was installed" }
    Expand-Archive -LiteralPath $UvArchive -DestinationPath (Join-Path $Work 'uv')
    $Uv = Join-Path $Work 'uv\uv.exe'
    if (-not (Test-Path $Uv)) { Fail "uv.exe is not in uv-$UvTarget.zip" }
    Say "uv $UvVersion checked."

    # ---- upgrade: back up, stop --------------------------------------------------------------------------------------
    if (Test-Path $Pengumon) {
        Say "Pengumon is already installed ($(& $Pengumon version)): upgrading."
        # Windows PowerShell 5.1 turns a native program's stderr into a terminating error when it is redirected
        $ErrorActionPreference = 'Continue'
        $out = (& $Pengumon backup 2>&1 | Out-String).Trim()
        $backupExit = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
        if ($backupExit -eq 0) {
            Say "Backup of your data: $out"
        } elseif ($out -match 'no database') {
            Say 'No database yet, so nothing to back up.'
        } else {
            Fail "the backup failed, so nothing was changed: $out"
        }
        $ErrorActionPreference = 'Continue'
        & $Pengumon service uninstall *> $null
        $ErrorActionPreference = 'Stop'
    }

    # ---- install -----------------------------------------------------------------------------------------------------
    New-Item -ItemType Directory -Force -Path $App | Out-Null
    # Python is kept inside the app folder; no cache is left behind
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $App 'python'
    $env:UV_PYTHON_PREFERENCE = 'only-managed'
    $VenvPython = Join-Path $Venv 'Scripts\python.exe'

    Say "Installing Python $PythonVersion and the locked dependencies into $App ..."
    Invoke-Native $Uv @('--no-config', '--no-cache', 'python', 'install', $PythonVersion)
    Invoke-Native $Uv @('--no-config', '--no-cache', 'venv', '--clear', '--python', $PythonVersion, $Venv)
    Invoke-Native $Uv @('--no-config', '--no-cache', 'pip', 'install', '--python', $VenvPython, '--require-hashes', '--only-binary', ':all:', '--no-deps', '--compile-bytecode', '-r', (Join-Path $Work $Lock))
    Confirm-Sha256 $Wheel   # again, right before it is used
    Invoke-Native $Uv @('--no-config', '--no-cache', 'pip', 'install', '--python', $VenvPython, '--no-deps', '--compile-bytecode', (Join-Path $Work $Wheel))
    Invoke-Native $Pengumon @('version')

    # ---- service -----------------------------------------------------------------------------------------------------
    $ListenPort = 8000
    if ($Port -gt 0) { $ListenPort = $Port }
    if ($NoService) {
        Say "No service installed. Start it with: $Pengumon run"
        return
    }
    if ($Port -gt 0) {
        Invoke-Native $Pengumon @('service', 'install', '--port', "$Port")
    } else {
        Invoke-Native $Pengumon @('service', 'install')
    }

    Say "Waiting for Pengumon to answer on port $ListenPort ..."
    $Url = "http://127.0.0.1:$ListenPort/"
    $up = $false
    for ($i = 0; $i -lt 60 -and -not $up; $i++) {
        try {
            $null = Invoke-WebRequest -UseBasicParsing -Uri "${Url}api/health" -TimeoutSec 3
            $up = $true
        } catch {
            Start-Sleep -Seconds 2
        }
    }
    if (-not $up) { Fail "it has not answered after 2 minutes. Check it with: $Pengumon service status" }
    Say "Pengumon is running: $Url"
    if (-not $NoBrowser) { Start-Process $Url }
    Say "Check it any time with: $Pengumon service status"
} finally {
    Remove-Item -Recurse -Force -LiteralPath $Work -ErrorAction SilentlyContinue
}
