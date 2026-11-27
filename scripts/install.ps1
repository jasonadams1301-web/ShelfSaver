<#
.SYNOPSIS
  Installs, upgrades or removes ShelfSaver on this PC. Run as administrator.

.DESCRIPTION
  Install / upgrade (same command): builds the new version beside the old one, then stops
  ShelfSaver, swaps the program folder and starts it again. If anything goes wrong the old
  version is put back and started. Your settings, history and logs in the data folder are
  not touched.

  -Uninstall   Stops ShelfSaver and removes its scheduled task, firewall rule and program
               folder. The data folder is kept unless -RemoveData is also given.
  -RemoveData  With -Uninstall only: also delete the data folder (settings, history, login).
  -Python      The python.exe to build with, instead of searching for one. ShelfSaver's Update
               button passes the one the running copy was built from, because the background
               (SYSTEM) account may not find a Python installed for one person.

  Your video libraries, holding folders and scratch disks are never touched.
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$RemoveData,
    [string]$Python
)

$ErrorActionPreference = 'Stop'

# --- Administrator check comes first: nothing else runs without it. ---------------------------
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host ''
    Write-Host 'ShelfSaver setup needs to run as administrator.' -ForegroundColor Red
    Write-Host 'Close this window, right-click PowerShell, choose "Run as administrator",'
    Write-Host 'then run the same command again.'
    exit 1
}

$TaskName = 'ShelfSaver'
$RuleName = 'ShelfSaver'
$Port = 8642
$AppDir = Join-Path $env:ProgramFiles 'ShelfSaver'
$NewDir = "$AppDir.new"
$OldDir = "$AppDir.old"
$DataDir = Join-Path $env:ProgramData 'ShelfSaver'
$VenvPython = Join-Path $AppDir 'venv\Scripts\python.exe'
$NewVenvPython = Join-Path $NewDir 'venv\Scripts\python.exe'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$ConfigPath = Join-Path $DataDir 'config.json'
$ImportCheck = 'import shelfsaver, uvicorn, fastapi, watchdog, mcp'
$AllowedSids = @('S-1-5-18', 'S-1-5-32-544')   # SYSTEM and Administrators

function Say($text) { Write-Host $text }
function Step($text) { Write-Host ''; Write-Host "== $text" -ForegroundColor Cyan }
function Warn($text) { Write-Host "NOTE: $text" -ForegroundColor Yellow }
function Fail($text) { throw $text }

function Invoke-Native {
    # Runs a program and fails with a plain message when it returns an error.
    param([string]$What, [string]$Exe, [string[]]$ArgList)
    & $Exe @ArgList
    if ($LASTEXITCODE -ne 0) { Fail "$What did not finish (exit code $LASTEXITCODE)." }
}

function Test-Inside([string]$child, [string]$parent) {
    $c = [IO.Path]::GetFullPath($child).TrimEnd('\') + '\'
    $p = [IO.Path]::GetFullPath($parent).TrimEnd('\') + '\'
    return $c.StartsWith($p, [StringComparison]::OrdinalIgnoreCase)
}

# Refuse to run from inside a folder this script is about to replace. Nothing has been touched yet.
foreach ($guarded in $AppDir, $NewDir, $OldDir, $DataDir) {
    # The Update button unpacks the new release into the data folder's update area, which only
    # SYSTEM and Administrators can write, and runs setup from there. Nothing in it is replaced.
    if ($guarded -eq $DataDir -and (Test-Inside $RepoRoot (Join-Path $DataDir 'update'))) { continue }
    if (Test-Inside $RepoRoot $guarded) {
        Write-Host ''
        Write-Host "This copy of ShelfSaver is inside $guarded, which setup replaces." -ForegroundColor Red
        Write-Host 'Copy the ShelfSaver folder somewhere else (for example your Downloads folder),'
        Write-Host 'then run setup from there. Nothing was changed.'
        exit 1
    }
}

# --- Stop / remove helpers ---------------------------------------------------------------------
function Get-AppProcessIds {
    # Only programs that live in the ShelfSaver program folder, so a development copy is left alone.
    $prefix = $AppDir + '\'
    $found = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue |
        Where-Object {
            ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) -or
            ($_.CommandLine -and $_.CommandLine.IndexOf($prefix, [StringComparison]::OrdinalIgnoreCase) -ge 0)
        }
    return @($found | ForEach-Object { $_.ProcessId })
}

function Stop-ShelfSaver {
    # Disable first so Windows can't restart it while we work; callers re-enable when done.
    $hasTask = [bool](Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)
    if ($hasTask) { Disable-ScheduledTask -TaskName $TaskName | Out-Null }
    # End the whole process tree first, from the launcher in the program folder (the real
    # interpreter lives in the base Python folder). Stopping the task first could kill the launcher
    # and leave its children running with no way to find them.
    $ids = Get-AppProcessIds
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    foreach ($id in $ids) { & taskkill.exe /PID $id /T /F 2>&1 | Out-Null }   # already gone is fine
    $ErrorActionPreference = $previous
    if ($hasTask) { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }
    for ($i = 0; $i -lt 30; $i++) {
        if ((Get-AppProcessIds).Count -eq 0) { return }
        Start-Sleep -Seconds 1
    }
    Fail 'ShelfSaver would not stop. Restart the PC and run this again.'
}

function Enable-Task {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Enable-ScheduledTask -TaskName $TaskName | Out-Null
    }
}

function Remove-Directory($path) {
    # Retry briefly: a just-stopped program can hold files for a moment.
    for ($i = 0; $i -lt 10; $i++) {
        if (-not (Test-Path -LiteralPath $path)) { return }
        try { Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction Stop; return }
        catch { Start-Sleep -Seconds 1 }
    }
    if (Test-Path -LiteralPath $path) { Fail "Could not remove the folder $path. Close anything using it and try again." }
}

function Test-PortOpen {
    $client = New-Object Net.Sockets.TcpClient
    try { $client.Connect('127.0.0.1', $Port); return $true } catch { return $false } finally { $client.Close() }
}

function Wait-ForPort([int]$seconds) {
    for ($i = 0; $i -lt $seconds; $i++) {
        if (Test-PortOpen) { return $true }
        if ($i -gt 0 -and $i % 10 -eq 0) { Say "Still waiting for ShelfSaver to start ($i seconds)..." }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Rename-WithRetry([string]$from, [string]$newName) {
    # A just-stopped program can hold a folder for a few seconds.
    for ($i = 0; $i -lt 10; $i++) {
        try { Rename-Item -LiteralPath $from -NewName $newName -ErrorAction Stop; return }
        catch { if ($i -eq 9) { throw }; Start-Sleep -Seconds 1 }
    }
}

# --- Uninstall ---------------------------------------------------------------------------------
if ($Uninstall) {
    Step 'Removing ShelfSaver'
    Stop-ShelfSaver
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Say 'Removed the startup task.'
    }
    if (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue) {
        Remove-NetFirewallRule -DisplayName $RuleName
        Say 'Removed the firewall rule.'
    }
    if (Get-ScheduledTask -TaskName 'ShelfSaver-Update' -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName 'ShelfSaver-Update' -Confirm:$false
    }
    foreach ($dir in $AppDir, $NewDir, $OldDir) { Remove-Directory $dir }
    Remove-Directory (Join-Path $DataDir 'update')
    Say "Removed the program folder ($AppDir)."
    if ($RemoveData) {
        Remove-Directory $DataDir
        Say "Removed the data folder ($DataDir), including settings and history."
    } else {
        Say ''
        Say "Your settings, history and login were KEPT in $DataDir."
        Say 'To delete them as well, run the uninstall again with -RemoveData.'
    }
    Say 'Your video libraries, holding folders and scratch disks were not touched.'
    exit 0
}

if ($RemoveData) { Fail '-RemoveData only works together with -Uninstall.' }

# --- Install / upgrade -------------------------------------------------------------------------
if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'shelfsaver\__main__.py'))) {
    Fail "Can't find the ShelfSaver files next to this script. Run it from the downloaded ShelfSaver folder."
}

function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

function Test-SystemRuns {
    # Really runs the program as SYSTEM (what the startup task will use) and checks it worked.
    param([string]$Exe, [string]$ExeArgs = '', [string]$PyCode = '')
    $checkDir = Join-Path $DataDir 'check'
    New-Item -ItemType Directory -Force -Path $checkDir | Out-Null
    $out = Join-Path $checkDir 'out.txt'
    Remove-Item -LiteralPath $out -Force -ErrorAction SilentlyContinue
    if ($PyCode) {
        $script = Join-Path $checkDir 'check.py'
        [IO.File]::WriteAllText($script, $PyCode, (New-Object Text.UTF8Encoding($false)))
        $ExeArgs = '"{0}"' -f $script
    }
    $name = 'ShelfSaver-Check'
    $cmdArgs = '/c ""{0}" {1} > "{2}" 2>&1 && echo SHELFSAVER_OK>> "{2}""' -f $Exe, $ExeArgs, $out
    $action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $cmdArgs
    $who = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    try {
        Register-ScheduledTask -TaskName $name -Action $action -Principal $who -Settings $settings -Force | Out-Null
        Start-ScheduledTask -TaskName $name
        for ($i = 0; $i -lt 60; $i++) {
            Start-Sleep -Seconds 1
            if ((Get-ScheduledTask -TaskName $name).State -ne 'Running' -and (Test-Path -LiteralPath $out)) { break }
        }
        $text = if (Test-Path -LiteralPath $out) { Get-Content -LiteralPath $out -Raw } else { '' }
        return [bool]($text -match 'SHELFSAVER_OK')
    } finally {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $checkDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Resolve-RealPath([string]$path) {
    # A WinGet "link" is a shortcut SYSTEM can't see; follow it to the real file.
    $item = Get-Item -LiteralPath $path
    if ($item.LinkType -and $item.Target) {
        $target = @($item.Target)[0]
        if (-not [IO.Path]::IsPathRooted($target)) { $target = Join-Path $item.DirectoryName $target }
        return [IO.Path]::GetFullPath($target)
    }
    return $item.FullName
}

function Find-Python {
    $found = @()
    Get-ChildItem -Path (Join-Path $env:ProgramFiles 'Python3*') -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | ForEach-Object { $found += (Join-Path $_.FullName 'python.exe') }
    Get-Command python.exe -All -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -and $_.Source -notmatch '\\WindowsApps\\' } |
        ForEach-Object { $found += $_.Source }
    foreach ($candidate in $found) {
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        $ok = & $candidate -c 'import sys; print(sys.version_info >= (3, 10))' 2>$null
        if ($ok -eq 'True') { return (Resolve-RealPath $candidate) }
    }
    return $null
}

function Install-WithWinget([string]$id, [string]$label) {
    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        Fail "$label is needed but Windows' app installer (winget) isn't available. Install $label yourself, then run this again."
    }
    Say "Installing $label for all users..."
    $common = @('install', '--id', $id, '-e', '--silent',
                '--accept-package-agreements', '--accept-source-agreements')
    & winget.exe @common '--scope' 'machine'
    if ($LASTEXITCODE -ne 0) {
        Warn "$label could not be installed for all users. Trying again for this account only."
        & winget.exe @common
    }
    Update-SessionPath
}

function Get-AclOffenders {
    # Items under the data folder that still let anyone but SYSTEM and Administrators in.
    $items = @(Get-Item -LiteralPath $DataDir -Force) +
        @(Get-ChildItem -LiteralPath $DataDir -Recurse -Force -ErrorAction SilentlyContinue)
    foreach ($item in $items) {
        $acl = Get-Acl -LiteralPath $item.FullName
        $bad = @($acl.Access | Where-Object {
            $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -notin $AllowedSids })
        if ($bad.Count -gt 0) { $item }
    }
}

function Protect-DataFolder {
    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
    # SYSTEM and Administrators only: the folder holds the login and access tokens.
    Invoke-Native 'Locking the data folder' icacls.exe @($DataDir, '/inheritance:r', '/grant:r',
        '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F') | Out-Null
    if (@(Get-ChildItem -LiteralPath $DataDir -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -ne 'check' }).Count -gt 0) {
        Invoke-Native 'Locking the files in the data folder' icacls.exe @("$DataDir\*", '/inheritance:e',
            '/T', '/C', '/Q') | Out-Null
    }
    # Read it back, and remove anything else that still has access.
    foreach ($item in @(Get-AclOffenders)) {
        $acl = Get-Acl -LiteralPath $item.FullName
        $isRoot = ($item.FullName.TrimEnd('\') -eq $DataDir.TrimEnd('\'))
        $acl.SetAccessRuleProtection($isRoot, $false)
        foreach ($rule in @($acl.Access)) {
            $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
            if ($sid -notin $AllowedSids -and -not $rule.IsInherited) { [void]$acl.RemoveAccessRule($rule) }
        }
        Set-Acl -LiteralPath $item.FullName -AclObject $acl
    }
    $left = @(Get-AclOffenders)
    if ($left.Count -gt 0) {
        Fail ("Could not make the data folder private. Still open to other users: " + (($left | Select-Object -First 3 | ForEach-Object { $_.FullName }) -join ', '))
    }
    Say "Only administrators can open $DataDir"
}

function Find-ToolCandidates([string]$name, $config) {
    # Order: what config.json already says, then PATH, then WinGet's own folders.
    $candidates = @()
    $configured = $config.PSObject.Properties[$name]
    if ($configured -and $configured.Value) { $candidates += [string]$configured.Value }
    Get-Command "$name.exe" -All -ErrorAction SilentlyContinue | ForEach-Object { $candidates += $_.Source }
    $roots = @((Join-Path $env:ProgramFiles 'WinGet\Packages'),
               (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'))
    foreach ($pkgs in $roots) {
        if (Test-Path -LiteralPath $pkgs) {
            Get-ChildItem -Path $pkgs -Recurse -Filter "$name.exe" -ErrorAction SilentlyContinue |
                ForEach-Object { $candidates += $_.FullName }
        }
    }
    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c)) { Resolve-RealPath $c }
    }
}

function Find-Tool([string]$name, $config) {
    return @(Find-ToolCandidates $name $config) | Select-Object -First 1
}

function Test-HasGpuEncoder([string]$exe) {
    # An FFmpeg bundled with another app may be unable to use the graphics card at all. Any of
    # NVIDIA's, AMD's or Intel's encoder will do: ShelfSaver picks whichever works when it starts.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $listing = & $exe -hide_banner -encoders 2>$null | Out-String
        return ($LASTEXITCODE -eq 0 -and $listing -match '\bhevc_(nvenc|amf|qsv)\b')
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Find-EncodingFFmpeg($config) {
    foreach ($candidate in @(Find-ToolCandidates 'ffmpeg' $config)) {
        if ($candidate -and (Test-HasGpuEncoder $candidate)) { return $candidate }
    }
    return $null
}

function Get-UsableTool([string]$name, $config) {
    $path = if ($name -eq 'ffmpeg') { Find-EncodingFFmpeg $config } else { Find-Tool $name $config }
    if (-not $path) { return $null }
    $inApp = $path.StartsWith($AppDir + '\', [StringComparison]::OrdinalIgnoreCase)
    if (-not $inApp -and (Test-SystemRuns $path '-version')) { return $path }
    # Installed for one user only (or kept in the old program folder). These are single-file
    # programs, so give the new program folder its own copy.
    $bin = Join-Path $NewDir 'bin'
    New-Item -ItemType Directory -Force -Path $bin | Out-Null
    $copy = Join-Path $bin "$name.exe"
    Copy-Item -LiteralPath $path -Destination $copy -Force
    if (Test-SystemRuns $copy '-version') {
        if (-not $inApp) { Warn "$name was only installed for one user, so ShelfSaver keeps its own copy in $AppDir\bin." }
        return (Join-Path (Join-Path $AppDir 'bin') "$name.exe")
    }
    return $null
}

# --- The install itself ------------------------------------------------------------------------
$script:oldMoved = $false
$script:newInPlace = $false
$script:configWritten = $false
$script:configBackup = $null
$script:failure = $null
$script:succeeded = $false

try {
    # Leftovers from a run that was cut off. If the swap was interrupted, put the old version back.
    $oldIsComplete = Test-Path -LiteralPath (Join-Path $OldDir 'venv\Scripts\python.exe')
    if ($oldIsComplete -and -not (Test-Path -LiteralPath $VenvPython)) {
        if (Test-Path -LiteralPath $AppDir) { Remove-Directory $AppDir }   # half-built, nothing to keep
        Rename-WithRetry $OldDir (Split-Path -Leaf $AppDir)
    }
    Remove-Directory $NewDir
    Remove-Directory $OldDir

    Step 'Preparing the data folder'
    Protect-DataFolder

    Step 'Checking Python'
    $Python = if ($Python -and (Test-Path -LiteralPath $Python -PathType Leaf)) {
        Resolve-RealPath $Python
    } else {
        Find-Python
    }
    if (-not $Python) {
        Install-WithWinget 'Python.Python.3.12' 'Python'
        $Python = Find-Python
    }
    if (-not $Python) { Fail 'Python could not be found or installed. Install Python 3.10 or newer from python.org, then run this again.' }
    Say "Using Python at $Python"
    if (-not (Test-SystemRuns $Python '--version')) {
        Fail "Python at $Python can't be used by the background service (it lives in a personal folder). Install Python for all users from python.org and run this again."
    }

    New-Item -ItemType Directory -Force -Path $NewDir | Out-Null

    $config = New-Object PSObject
    if (Test-Path -LiteralPath $ConfigPath) {
        $script:configBackup = [IO.File]::ReadAllText($ConfigPath, (New-Object Text.UTF8Encoding($false)))
        try {
            $existing = $script:configBackup | ConvertFrom-Json
            if ($existing -is [PSCustomObject]) { $config = $existing } else { Fail 'not settings' }
        } catch {
            Move-Item -LiteralPath $ConfigPath -Destination "$ConfigPath.bad" -Force
            $script:configBackup = $null
            Warn 'The old config.json could not be read. It was kept as config.json.bad and a new one will be written.'
        }
    }

    Step 'Checking FFmpeg'
    if (-not (Find-EncodingFFmpeg $config) -or -not (Find-Tool 'ffprobe' $config)) {
        Install-WithWinget 'Gyan.FFmpeg' 'FFmpeg'
    }
    if ((Find-Tool 'ffmpeg' $config) -and -not (Find-EncodingFFmpeg $config)) {
        Fail "The FFmpeg on this PC can't convert video on a graphics card (NVIDIA, AMD or Intel). Install the full version by running: winget install Gyan.FFmpeg. Then run this again."
    }
    $ffmpeg = Get-UsableTool 'ffmpeg' $config
    $ffprobe = Get-UsableTool 'ffprobe' $config
    if (-not $ffmpeg -or -not $ffprobe) {
        Fail 'FFmpeg could not be found in a place the background service can use. Install FFmpeg for all users, then run this again.'
    }
    Say "Using $ffmpeg"
    Say "Using $ffprobe"

    Step 'Building the new version (the current one keeps running meanwhile)'
    $staging = Join-Path ([IO.Path]::GetTempPath()) ('shelfsaver-setup-' + [Guid]::NewGuid().ToString('N'))
    try {
        New-Item -ItemType Directory -Force -Path $staging | Out-Null
        Copy-Item -LiteralPath (Join-Path $RepoRoot 'shelfsaver') -Destination $staging -Recurse
        foreach ($file in 'pyproject.toml', 'LICENSE', 'README.md') {
            $src = Join-Path $RepoRoot $file
            if (Test-Path -LiteralPath $src) { Copy-Item -LiteralPath $src -Destination $staging }
        }
        Get-ChildItem -LiteralPath $staging -Recurse -Directory -Filter '__pycache__' |
            Remove-Item -Recurse -Force
        Say 'Setting up (this downloads a few packages and can take a minute)...'
        Invoke-Native 'Creating the Python environment' $Python @('-m', 'venv', (Join-Path $NewDir 'venv'))
        Invoke-Native 'Installing ShelfSaver' $NewVenvPython @('-m', 'pip', 'install', '--quiet',
            '--disable-pip-version-check', '--no-warn-script-location', $staging)
    } finally {
        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    }
    # The comparison-clip tool is installed beside the app.
    New-Item -ItemType Directory -Force -Path (Join-Path $NewDir 'scripts') | Out-Null
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'make_compare_clips.py') -Destination (Join-Path $NewDir 'scripts')
    if (-not (Test-SystemRuns $NewVenvPython -PyCode $ImportCheck)) {
        Fail 'The new version could not start under the background service account.'
    }

    Step 'Switching to the new version'
    Stop-ShelfSaver
    if (Test-PortOpen) {
        $owner = ''
        try {
            $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | Select-Object -First 1
            $owner = ' (' + (Get-Process -Id $conn.OwningProcess -ErrorAction Stop).ProcessName + ')'
        } catch { }
        Fail "Port $Port is already being used by another program$owner, for example a development copy of ShelfSaver. Close it, then run setup again."
    }
    if (Test-Path -LiteralPath $AppDir) {
        Rename-WithRetry $AppDir (Split-Path -Leaf $OldDir)
        $script:oldMoved = $true
    }
    Rename-WithRetry $NewDir (Split-Path -Leaf $AppDir)
    $script:newInPlace = $true
    if (-not (Test-SystemRuns $VenvPython -PyCode $ImportCheck)) {
        Fail 'The new version could not start from its final folder.'
    }

    $script:configWritten = $true
    $config | Add-Member -NotePropertyName ffmpeg -NotePropertyValue $ffmpeg -Force
    $config | Add-Member -NotePropertyName ffprobe -NotePropertyValue $ffprobe -Force
    [IO.File]::WriteAllText($ConfigPath, ($config | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding($false)))
    Say "Saved settings to $ConfigPath"

    Step 'Setting ShelfSaver to start with the PC'
    # Quotes keep "C:\Program Files" in one piece.
    $action = New-ScheduledTaskAction -Execute ('"{0}"' -f $VenvPython) -Argument '-m shelfsaver' -WorkingDirectory $AppDir
    $trigger = New-ScheduledTaskTrigger -AtStartup
    # Also check every 5 minutes: if the app has stopped it comes back, and if it is running the
    # new start is ignored (and the app refuses a second copy anyway). The startup trigger's
    # repeat only begins at the next boot, so a second trigger starts the repeat right away.
    $nowTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
        -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 9999)
    $trigger.Repetition = $nowTrigger.Repetition
    $who = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($trigger, $nowTrigger) -Principal $who `
        -Settings $settings -Description 'ShelfSaver: converts video libraries to save space.' -Force | Out-Null
    Say 'Done.'

    Step 'Allowing access from other devices'
    if (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue) {
        Remove-NetFirewallRule -DisplayName $RuleName
    }
    New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow -Protocol TCP `
        -LocalPort $Port -Profile Domain,Private,Public | Out-Null
    Say "Port $Port is open on Domain, Private and Public networks."

    Step 'Starting ShelfSaver'
    Start-ScheduledTask -TaskName $TaskName
    if (-not (Wait-ForPort 90)) { Fail 'The new version did not start.' }
    # Something else could be answering on the port, so also confirm the installed app is alive.
    Start-Sleep -Seconds 3
    $alive = ((Get-AppProcessIds).Count -gt 0) -and
        ((Get-ScheduledTask -TaskName $TaskName).State -eq 'Running') -and (Test-PortOpen)
    if (-not $alive) { Fail 'The new version started and then stopped again.' }

    # The new version works; from here on nothing may undo it.
    $script:succeeded = $true
    try { Remove-Directory $OldDir }
    catch { Warn "Could not delete the old copy in $OldDir. It is harmless and will be removed next time setup runs." }
} catch {
    $script:failure = $_.Exception.Message
}

if (-not $script:succeeded) {
    Write-Host ''
    Write-Host "Setup did not finish: $($script:failure)" -ForegroundColor Red
    try {
        if ($script:newInPlace -or $script:oldMoved) {
            Stop-ShelfSaver
            if ($script:newInPlace) { Remove-Directory $AppDir }
            if ($script:oldMoved) { Rename-WithRetry $OldDir (Split-Path -Leaf $AppDir) }
            if ($script:configWritten) {
                if ($null -ne $script:configBackup) {
                    [IO.File]::WriteAllText($ConfigPath, $script:configBackup, (New-Object Text.UTF8Encoding($false)))
                } else {
                    Remove-Item -LiteralPath $ConfigPath -Force -ErrorAction SilentlyContinue
                }
            }
        }
        Remove-Directory $NewDir
    } catch {
        Write-Host "Could not fully undo the change: $($_.Exception.Message)" -ForegroundColor Red
    }
    try {
        Enable-Task
        if ((Test-Path -LiteralPath $VenvPython) -and (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
            Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        }
    } catch { }
    if (Wait-ForPort 60) {
        Write-Host 'ShelfSaver IS running (the version that was already installed).' -ForegroundColor Yellow
    } else {
        Write-Host 'ShelfSaver is NOT running right now.' -ForegroundColor Yellow
        if (-not (Test-Path -LiteralPath $VenvPython)) { Write-Host 'No working version is installed.' }
        else { Write-Host 'It will try to start again by itself within a few minutes, or after a restart.' }
    }
    Write-Host "Your settings and history in $DataDir are safe and were not changed."
    Write-Host 'Fix the problem above and run setup again.'
    exit 1
}

Enable-Task

$ip = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.)' -and $_.PrefixOrigin -in 'Dhcp', 'Manual' } |
    Select-Object -First 1 -ExpandProperty IPAddress

Write-Host ''
Write-Host 'ShelfSaver is installed and running.' -ForegroundColor Green
Say 'Open ShelfSaver in a web browser:'
Say "  On this PC:        http://localhost:$Port"
if ($ip) { Say "  From another device: http://${ip}:$Port" }
Say ''
Say "If the other device can't connect, check that nothing else on this PC is blocking port $Port."
Say "Your settings and history live in $DataDir and are kept when you update or uninstall."
Say "The comparison-clip tool is installed at $AppDir\scripts\make_compare_clips.py"
exit 0
