# Easel launcher.
#
# Why this exists: this machine has a corrupted machine-level COMSPEC
#   COMSPEC = C:\WINDOWS\system32\cmd.exe;<...>\ffmpeg-master...\bin
# instead of just C:\WINDOWS\system32\cmd.exe. Windows uses that variable
# verbatim to locate the command interpreter, so any process that shells out to
# cmd.exe (npm lifecycle scripts, Node child_process, many CLI wrappers) dies
# with `ENOENT ... errno -4058`. Repairing it machine-wide needs administrator
# rights, so this launcher repairs it for the Easel process tree only.
#
# Usage:
#   .\easel.ps1 web          # start the Web workspace (default port 7860)
#   .\easel.ps1 doctor       # check the installed environment
#   .\easel.ps1 ping         # model connectivity test
#   .\easel.ps1 gateway status|start|stop|restart
#   .\easel.ps1 <anything>   # forwarded verbatim to the easel CLI
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$EaselArgs
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Easel = Join-Path $Root '.venv\Scripts\easel.exe'

# --- Repair COMSPEC for this process tree -------------------------------------
$cmdExe = Join-Path $env:SystemRoot 'System32\cmd.exe'
if (-not (Test-Path $cmdExe)) { $cmdExe = 'C:\WINDOWS\system32\cmd.exe' }
$env:COMSPEC = $cmdExe
$env:ComSpec = $cmdExe

# --- UTF-8 mode (setup.ps1 does the same) ------------------------------------
# On a Chinese Windows install the OEM code page is 936, and Python picks its
# stdout encoding from it. web/app.py prints a banner containing "✦", which then
# dies with UnicodeEncodeError: 'gbk' codec can't encode character '\u2726'.
# PYTHONUTF8=1 makes Python use UTF-8 for stdio regardless of the code page.
$env:PYTHONUTF8 = '1'

# --- Load .env into this process ---------------------------------------------
# Some provider credentials resolve from the *environment*, not from
# openclaw.json -- Tavily's web-search key is one:
# plugins.entries.tavily.config.webSearch.apiKey falls back to TAVILY_API_KEY.
# Easel's web app only *reads* .env (settings UI + secret scrubbing) and does not
# export it to the gateway it spawns; setup.ps1 likewise only writes selected
# keys into openclaw.json. So whoever starts the gateway must export .env, which
# is what this block does. Without it a web_search provider reports
# "provider_error: Search failed. Check this search provider's configuration".
$envFile = Join-Path $Root '.env'
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile -Encoding UTF8) {
        $s = $line.Trim()
        if (-not $s -or $s.StartsWith('#') -or -not $s.Contains('=')) { continue }
        $kv = $s.Split('=', 2)
        $k = $kv[0].Trim()
        $v = $kv[1].Trim().Trim('"').Trim("'")
        if (-not $k) { continue }
        # Never clobber a value the caller already set explicitly.
        if (-not [Environment]::GetEnvironmentVariable($k, 'Process')) {
            [Environment]::SetEnvironmentVariable($k, $v, 'Process')
        }
    }
}

# --- Make freshly installed tools visible without reopening the shell ---------
foreach ($dir in @(
        (Join-Path $env:APPDATA 'npm'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links'),
        (Join-Path $env:ProgramFiles 'Git\cmd')
    )) {
    if ((Test-Path $dir) -and (($env:Path -split ';') -notcontains $dir)) {
        $env:Path = "$env:Path;$dir"
    }
}
# winget-installed ffmpeg lives under a versioned package directory.
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    $ff = Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages') -Recurse -Filter 'ffmpeg.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($ff) { $env:Path = "$env:Path;$($ff.Directory.FullName)" }
}

if ($EaselArgs.Count -eq 0) { $EaselArgs = @('web') }

if (-not (Test-Path $Easel)) {
    Write-Host "[easel] CLI not found at $Easel" -ForegroundColor Red
    Write-Host "[easel] Run setup first:  .\setup.ps1 -OpenClaw" -ForegroundColor Yellow
    exit 1
}

Set-Location $Root
Write-Host "[easel] $($EaselArgs -join ' ')" -ForegroundColor Cyan
& $Easel @EaselArgs
exit $LASTEXITCODE
