# TokenAlarm installer for Windows (PowerShell 5.1+). ASCII only on purpose.
#
#   Install / update (same command):
#     irm https://raw.githubusercontent.com/JMH-Chem/ai-token-alarm-and-auto-start/main/install.ps1 | iex
#   Uninstall:
#     $env:TOKENALARM_UNINSTALL=1; irm https://raw.githubusercontent.com/JMH-Chem/ai-token-alarm-and-auto-start/main/install.ps1 | iex
#
# A piped script cannot take parameters, so options are environment variables:
#   TOKENALARM_SOURCE    where to install from: a local folder, a URL, or "pypi" (default: GitHub main branch)
#   TOKENALARM_NO_SETUP  1 = do not start the setup wizard after a fresh install
#   TOKENALARM_NO_TRAY   1 = install without the optional tray icon
#   TOKENALARM_UNINSTALL 1 = remove TokenAlarm (autostart, Claude Code hooks, program; data folder kept)
& {
    # 'Continue': in Windows PowerShell 5.1, 'Stop' turns any redirected stderr line of a native program
    # (uv prints progress there) into a terminating error. Failures are detected via $LASTEXITCODE instead.
    $ErrorActionPreference = 'Continue'
    $repo = 'https://github.com/JMH-Chem/ai-token-alarm-and-auto-start'
    $defaultSource = "$repo/archive/refs/heads/main.zip"

    function Say([string]$m) { Write-Host "[tokenalarm] $m" -ForegroundColor Cyan }

    try {
        # 1) uv (installs a private Python for us; no admin rights needed)
        $uv = $null
        $cmd = Get-Command uv -ErrorAction SilentlyContinue
        if ($cmd) { $uv = $cmd.Source }
        if (-not $uv) {
            foreach ($c in @("$HOME\.local\bin\uv.exe", "$HOME\.cargo\bin\uv.exe")) {
                if (Test-Path $c) { $uv = $c; break }
            }
        }
        if (-not $uv) {
            if ($env:TOKENALARM_UNINSTALL) { Say 'uv is not installed - nothing to uninstall.'; return }
            Say 'Installing uv (user scope)...'
            powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
            if ($LASTEXITCODE -ne 0) { throw 'uv installation failed' }
            $uv = "$HOME\.local\bin\uv.exe"
            if (-not (Test-Path $uv)) { throw "uv was installed but not found at $uv" }
        }

        $toolBin = (& $uv tool dir --bin).Trim()
        $ta = Join-Path $toolBin 'tokenalarm.exe'
        $installed = Test-Path $ta

        # 2) uninstall mode
        if ($env:TOKENALARM_UNINSTALL) {
            if ($installed) {
                Say 'Removing autostart and Claude Code integration...'
                & $ta uninstall --yes
            }
            & $uv tool uninstall tokenalarm
            Say 'TokenAlarm removed. Your data folder was kept (delete %LOCALAPPDATA%\tokenalarm to remove it).'
            return
        }

        # 3) install or upgrade
        if ($installed) {
            Say 'Existing installation found - stopping it before the update...'
            & $ta stop | Out-Null
        }
        $extras = '[tray]'
        if ($env:TOKENALARM_NO_TRAY) { $extras = '' }
        $src = $env:TOKENALARM_SOURCE
        if (-not $src) { $src = $defaultSource }
        if ($src -eq 'pypi') {
            $spec = "tokenalarm$extras"
        } elseif (Test-Path $src) {
            $uri = ([System.Uri](Resolve-Path $src).Path).AbsoluteUri
            $spec = "tokenalarm$extras @ $uri"
        } else {
            $spec = "tokenalarm$extras @ $src"
        }
        Say "Installing $spec ..."
        # A uv-managed Python avoids the Microsoft Store Python (which hides AppData writes).
        # Newer uv spells this --managed-python; older uv only knows --python-preference.
        $help = (& $uv tool install --help) -join "`n"
        if ($help -match '--managed-python') { $pyOpt = @('--managed-python') }
        else { $pyOpt = @('--python-preference', 'only-managed') }
        & $uv tool install --force --reinstall --python 3.12 @pyOpt $spec
        if ($LASTEXITCODE -ne 0) { throw 'uv tool install failed' }
        & $uv tool update-shell *> $null
        if (($env:Path -split ';') -notcontains $toolBin) { $env:Path = "$toolBin;$env:Path" }
        if (-not (Test-Path $ta)) { throw "tokenalarm.exe not found in $toolBin" }

        # 4) configure
        if ($installed) {
            Say 'Updating registrations...'
            & $ta setup --repair
        } elseif (-not $env:TOKENALARM_NO_SETUP) {
            & $ta setup
        }
        Say 'Done. In a new terminal: tokenalarm status   (help: tokenalarm --help)'
    } catch {
        Write-Host "[tokenalarm] ERROR: $_" -ForegroundColor Red
        Write-Host '[tokenalarm] See https://github.com/JMH-Chem/ai-token-alarm-and-auto-start#troubleshooting' -ForegroundColor Red
        if ($env:CI) { throw }  # make CI fail; interactive users keep their window
    }
}
