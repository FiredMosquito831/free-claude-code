param(
    [string] $Version = "",
    [switch] $VoiceNim,
    [switch] $VoiceLocal,
    [switch] $VoiceAll,
    [string] $TorchBackend = "",
    [switch] $DryRun,
    [switch] $Help,
    [Parameter(ValueFromRemainingArguments = $true)]
    [object[]] $RemainingArgs = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$FccRepo = "FiredMosquito831/free-claude-code"
$FccLatestReleaseUrl = "https://api.github.com/repos/$FccRepo/releases/latest"
$PythonVersion = "3.14.0"
$MinUvVersion = "0.11.0"
$UvInstallUrl = "https://astral.sh/uv/install.ps1"
# Set by Start-DeferredInstall when the app was running and the install was
# staged for completion after the user stops it.
$script:Deferred = $false

function Show-Usage {
    @"
Usage: install.ps1 [options]

Installs or updates Free Claude Code to the latest published release.

Installs a compatible uv if one is missing. It does not install Claude Code,
Codex, or Pi -- install whichever of those you use yourself.

Options:
  -Version VALUE         Install this exact release instead of the latest.
  -VoiceNim              Install NVIDIA NIM voice transcription support.
  -VoiceLocal            Install local Whisper voice transcription support.
  -VoiceAll              Install all voice transcription backends.
  -TorchBackend VALUE    Use a uv PyTorch backend, such as cu130. Requires local voice.
  -DryRun                Print commands without running them.
  -Help                  Show this help text.
"@
}

function Write-Step {
    param([string] $Message)

    Write-Host ""
    Write-Host "==> $Message"
}

function Format-Argument {
    param([string] $Value)

    if ($Value -match '^[A-Za-z0-9_./:@%+=,\[\]\\-]+$') {
        return $Value
    }

    return "'" + ($Value -replace "'", "''") + "'"
}

function Format-Command {
    param(
        [string] $FilePath,
        [string[]] $Arguments = @()
    )

    $parts = @($FilePath) + $Arguments
    return ($parts | ForEach-Object { Format-Argument ([string] $_) }) -join " "
}

function Invoke-NativeCommand {
    param(
        [string] $FilePath,
        [string[]] $Arguments = @()
    )

    $commandText = Format-Command -FilePath $FilePath -Arguments $Arguments
    Write-Host "+ $commandText"
    if ($DryRun) {
        return
    }

    $global:LASTEXITCODE = 0
    & $FilePath @Arguments
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "Command failed with exit code ${exitCode}: $commandText"
    }
}

function Invoke-NativeCapture {
    param(
        [string] $FilePath,
        [string[]] $Arguments = @()
    )

    $commandText = Format-Command -FilePath $FilePath -Arguments $Arguments
    Write-Host "+ $commandText"
    $global:LASTEXITCODE = 0
    $output = & $FilePath @Arguments
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "Command failed with exit code ${exitCode}: $commandText"
    }

    return ($output | Out-String).Trim()
}

function Get-ApplicationCommand {
    param([string] $Name)

    $commands = @(Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue)
    if ($commands.Count -eq 0) {
        return $null
    }

    return $commands[0]
}

function Get-PowerShellExecutable {
    param([string] $PowerShellHome = $PSHOME)

    $executableName = if ($PSVersionTable.PSEdition -eq "Core") {
        "pwsh.exe"
    }
    else {
        "powershell.exe"
    }
    $bundledExecutable = Join-Path $PowerShellHome $executableName
    if (Test-Path -LiteralPath $bundledExecutable -PathType Leaf) {
        return $bundledExecutable
    }

    $pathCommand = Get-ApplicationCommand ([IO.Path]::GetFileNameWithoutExtension($executableName))
    if ($pathCommand) {
        return $pathCommand.Source
    }

    throw "Unable to locate a PowerShell executable for the downloaded installer."
}

function Add-PathEntry {
    param([string] $PathEntry)

    if ([string]::IsNullOrWhiteSpace($PathEntry)) {
        return
    }

    $separator = [IO.Path]::PathSeparator
    $entries = @()
    if (-not [string]::IsNullOrEmpty($env:Path)) {
        $entries = $env:Path -split [regex]::Escape([string] $separator)
    }

    if ($entries -notcontains $PathEntry) {
        $env:Path = "$PathEntry$separator$env:Path"
    }
}

function Add-KnownBinDirectories {
    if (-not [string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
        Add-PathEntry (Join-Path $env:USERPROFILE ".local\bin")
    }
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        Add-PathEntry (Join-Path $env:LOCALAPPDATA "Programs\OpenAI\Codex\bin")
        Add-PathEntry (Join-Path $env:LOCALAPPDATA "pi-node\current")
    }
    if (-not [string]::IsNullOrWhiteSpace($env:APPDATA)) {
        Add-PathEntry (Join-Path $env:APPDATA "npm")
    }
}

function Add-PiBinDirectories {
    if ($DryRun) {
        return
    }

    Add-KnownBinDirectories
    $npm = Get-ApplicationCommand "npm"
    if (-not $npm) {
        return
    }

    $prefix = (& $npm.Source prefix -g 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($prefix)) {
        $prefix = (& $npm.Source config get prefix 2>$null | Out-String).Trim()
    }
    if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($prefix)) {
        Add-PathEntry $prefix
    }
}

function Invoke-DownloadedPowerShellInstaller {
    param(
        [string] $Url,
        [string] $Name,
        [switch] $NonInteractive
    )

    if ($DryRun) {
        Write-Host "+ irm $Url -OutFile <temporary-script>"
        $prefix = if ($NonInteractive) { "CODEX_NON_INTERACTIVE=1 " } else { "" }
        Write-Host "+ ${prefix}powershell -NoProfile -ExecutionPolicy Bypass -File <temporary-script>"
        return
    }

    $temporaryScript = Join-Path ([IO.Path]::GetTempPath()) ("fcc-install-" + [guid]::NewGuid().ToString("N") + ".ps1")
    try {
        Write-Host "+ irm $Url -OutFile $(Format-Argument $temporaryScript)"
        Invoke-RestMethod -Uri $Url -OutFile $temporaryScript -ErrorAction Stop
        if ((-not (Test-Path -LiteralPath $temporaryScript)) -or ((Get-Item -LiteralPath $temporaryScript).Length -eq 0)) {
            throw "The downloaded $Name installer was empty."
        }

        $powerShellPath = Get-PowerShellExecutable

        $hadNonInteractive = Test-Path Env:CODEX_NON_INTERACTIVE
        $previousNonInteractive = $env:CODEX_NON_INTERACTIVE
        try {
            if ($NonInteractive) {
                $env:CODEX_NON_INTERACTIVE = "1"
            }
            Invoke-NativeCommand -FilePath $powerShellPath -Arguments @(
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                $temporaryScript
            )
        }
        finally {
            if ($hadNonInteractive) {
                $env:CODEX_NON_INTERACTIVE = $previousNonInteractive
            }
            else {
                Remove-Item Env:CODEX_NON_INTERACTIVE -ErrorAction SilentlyContinue
            }
        }
    }
    finally {
        Remove-Item -LiteralPath $temporaryScript -Force -ErrorAction SilentlyContinue
    }
}

function Confirm-Application {
    param(
        [string] $CommandName,
        [string] $DisplayName
    )

    if ($DryRun) {
        Write-Host "+ $CommandName --version"
        return
    }

    $command = Get-ApplicationCommand $CommandName
    if (-not $command) {
        throw "$DisplayName was installed, but '$CommandName' is not available on PATH."
    }
    Invoke-NativeCommand -FilePath $command.Source -Arguments @("--version")
}

function Test-PiApplication {
    param($Command)

    try {
        $helpOutput = (& $Command.Source --help 2>$null | Out-String)
    }
    catch {
        return $false
    }
    return (
        $LASTEXITCODE -eq 0 -and
        $helpOutput.Contains("--extension") -and
        $helpOutput.Contains("--models")
    )
}

function Confirm-PiApplication {
    if ($DryRun) {
        Write-Host "+ pi --help (verify --extension and --models support)"
        Write-Host "+ pi --version"
        return
    }

    $command = Get-ApplicationCommand "pi"
    if (-not $command) {
        throw "Pi was installed, but 'pi' is not available on PATH."
    }
    if (-not (Test-PiApplication $command)) {
        throw "The 'pi' command at '$($command.Source)' is not a compatible Pi Coding Agent."
    }
    Invoke-NativeCommand -FilePath $command.Source -Arguments @("--version")
}

function Convert-UvVersionOutput {
    param([string] $Output)

    if ([string]::IsNullOrWhiteSpace($Output)) {
        return ""
    }

    if ($Output -match '(?m)(?:^|\s)(?:uv\s+)?(?<version>\d+\.\d+\.\d+(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?)\b') {
        return $Matches["version"]
    }

    return ""
}

function Get-UvVersion {
    param([string] $UvPath)

    $output = Invoke-NativeCapture -FilePath $UvPath -Arguments @("--version")
    $version = Convert-UvVersionOutput $output
    if ([string]::IsNullOrWhiteSpace($version)) {
        throw "uv is present, but 'uv --version' did not return a valid version."
    }

    return $version
}

function Test-UvVersionAtLeast {
    param(
        [string] $Version,
        [string] $Minimum
    )

    $normalizedVersion = (Convert-UvVersionOutput $Version) -replace '[-+].*$', ''
    $normalizedMinimum = (Convert-UvVersionOutput $Minimum) -replace '[-+].*$', ''
    if ([string]::IsNullOrWhiteSpace($normalizedVersion) -or [string]::IsNullOrWhiteSpace($normalizedMinimum)) {
        throw "Unable to compare uv versions."
    }

    return ([version] $normalizedVersion) -ge ([version] $normalizedMinimum)
}

function Confirm-Uv {
    if ($DryRun) {
        Write-Host "+ uv --version"
        return
    }

    $uvCommand = Get-ApplicationCommand "uv"
    if (-not $uvCommand) {
        throw "uv was installed, but it is not available on PATH."
    }

    $version = Get-UvVersion $uvCommand.Source
    if (-not (Test-UvVersionAtLeast -Version $version -Minimum $MinUvVersion)) {
        throw "uv $MinUvVersion or newer is required; found uv $version after installation."
    }
    Write-Host "Verified uv $version."
}

function Ensure-Uv {
    if ($DryRun) {
        if (Get-ApplicationCommand "uv") {
            Write-Host "+ uv --version"
            Write-Host "A compatible existing uv will be left unchanged; an obsolete one will be replaced by the standalone installer."
        }
        else {
            Write-Host "uv is not installed; the current standalone uv would be installed."
            Invoke-DownloadedPowerShellInstaller -Url $UvInstallUrl -Name "uv"
            Confirm-Uv
        }
        return
    }

    $uvCommand = Get-ApplicationCommand "uv"
    if ($uvCommand) {
        $version = Get-UvVersion $uvCommand.Source
        if (Test-UvVersionAtLeast -Version $version -Minimum $MinUvVersion) {
            Write-Host "uv $version already satisfies >=$MinUvVersion; leaving it unchanged."
            return
        }
        Write-Host "uv $version is below $MinUvVersion; installing the current standalone uv."
    }
    else {
        Write-Host "uv is not installed; installing the current standalone uv."
    }

    Invoke-DownloadedPowerShellInstaller -Url $UvInstallUrl -Name "uv"
    Add-KnownBinDirectories
    Confirm-Uv
}

function Resolve-Release {
    if ($Version) {
        $resolvedVersion = $Version -replace '^v', ''
        $resolvedSha256 = ""
    }
    else {
        # A GET that changes nothing, so it also runs during -DryRun and can
        # report the version that would actually install.
        Write-Host "+ irm $FccLatestReleaseUrl"
        try {
            $release = Invoke-RestMethod -Uri $FccLatestReleaseUrl -Headers @{
                "Accept" = "application/vnd.github+json"
            } -ErrorAction Stop
        }
        catch {
            throw "Could not reach the release feed to find the latest version: $($_.Exception.Message)"
        }
        $resolvedVersion = ([string] $release.tag_name) -replace '^v', ''
        if ([string]::IsNullOrWhiteSpace($resolvedVersion)) {
            throw "Could not read the latest release version from the release feed."
        }
        $resolvedSha256 = ""
        # GitHub publishes a sha256 digest per asset, so the download is still
        # verified even though no checksum is pinned in this script.
        $wheelAsset = @($release.assets | Where-Object { $_.name -like "*.whl" })
        if ($wheelAsset.Count -gt 0 -and $wheelAsset[0].digest) {
            $resolvedSha256 = ([string] $wheelAsset[0].digest) -replace '^sha256:', ''
        }
    }
    $wheelName = "my_claude_code-$resolvedVersion-py3-none-any.whl"
    # Returned rather than stored in script scope: when this file is run as a
    # scriptblock (the published `irm | iex` form) a function's `$script:`
    # writes are not visible to the rest of the script.
    return [pscustomobject]@{
        Version   = $resolvedVersion
        WheelName = $wheelName
        WheelUrl  = "https://github.com/$FccRepo/releases/download/v$resolvedVersion/$wheelName"
        Sha256    = $resolvedSha256
    }
}

function Get-VerifiedReleaseWheel {
    param([Parameter(Mandatory = $true)] $Release)

    if ($DryRun) {
        Write-Host "+ irm $($Release.WheelUrl) -OutFile <temporary-wheel>"
        if ($($Release.Sha256)) {
            Write-Host "+ verify SHA-256 $($Release.Sha256) for <temporary-wheel>"
        }
        else {
            Write-Host "+ verify the SHA-256 published for this release"
        }
        return "<verified-release-wheel>"
    }

    $temporaryDirectory = Join-Path (
        [IO.Path]::GetTempPath()
    ) ("fcc-wheel-" + [guid]::NewGuid().ToString("N"))
    $wheelPath = Join-Path $temporaryDirectory $($Release.WheelName)
    try {
        New-Item -ItemType Directory -Path $temporaryDirectory | Out-Null
        Write-Host "+ irm $($Release.WheelUrl) -OutFile $(Format-Argument $wheelPath)"
        Invoke-RestMethod -Uri $($Release.WheelUrl) -OutFile $wheelPath -ErrorAction Stop
        if (
            (-not (Test-Path -LiteralPath $wheelPath -PathType Leaf)) -or
            ((Get-Item -LiteralPath $wheelPath).Length -eq 0)
        ) {
            throw "The downloaded FCC release wheel was empty."
        }

        $actualSha256 = (Get-FileHash -LiteralPath $wheelPath -Algorithm SHA256).Hash
        if ($($Release.Sha256)) {
            if ($actualSha256 -ne $($Release.Sha256)) {
                throw "FCC release wheel checksum mismatch; refusing to install."
            }
            Write-Host "Verified FCC v$($Release.Version) release wheel SHA-256."
        }
        else {
            # Only reachable with -Version, where the release feed was not read
            # and no published digest is available to compare against.
            Write-Host "FCC v$($Release.Version) release wheel SHA-256: $actualSha256"
        }
        return $wheelPath
    }
    catch {
        Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force -ErrorAction SilentlyContinue
        throw
    }
}

function Get-PackageSpec {
    param([string] $PackageUrl)

    $includeNim = $VoiceNim
    $includeLocal = $VoiceLocal

    if ($VoiceAll) {
        $includeNim = $true
        $includeLocal = $true
    }

    if ($includeNim -and $includeLocal) {
        return "my-claude-code[voice,voice_local] @ $PackageUrl"
    }
    if ($includeNim) {
        return "my-claude-code[voice] @ $PackageUrl"
    }
    if ($includeLocal) {
        return "my-claude-code[voice_local] @ $PackageUrl"
    }
    return "my-claude-code @ $PackageUrl"
}

function Install-FreeClaudeCode {
    $release = Resolve-Release
    $wheelPath = Get-VerifiedReleaseWheel -Release $release
    $packageUrl = if ($DryRun) {
        "file:///<verified-release-wheel>"
    }
    else {
        ([Uri]::new($wheelPath)).AbsoluteUri
    }
    $packageSpec = Get-PackageSpec -PackageUrl $packageUrl
    $arguments = @(
        "tool",
        "install",
        "--force",
        "--refresh-package",
        "my-claude-code",
        "--python",
        $PythonVersion
    )
    if (-not [string]::IsNullOrWhiteSpace($TorchBackend)) {
        $arguments += @("--torch-backend", $TorchBackend)
    }
    $arguments += $packageSpec

    if ($DryRun) {
        return $release.Version
    }

    $uvCommand = Get-ApplicationCommand "uv"
    if (-not $uvCommand) {
        throw "uv is not available for the Free Claude Code installation."
    }
    $uvPath = $uvCommand.Source

    $running = @(Get-RunningLaunchers)
    if ($running.Count -gt 0) {
        # The app is live. Windows cannot replace the tool environment while a
        # process runs from it, so stage the verified wheel and let a detached
        # helper finish the install after the launchers exit. The user restarts
        # the app; the new version is picked up then. This mirrors how the
        # dashboard updater defers on Windows (release_updates.py) and how a
        # POSIX install already behaves (uv unlinks open files).
        return Start-DeferredInstall `
            -UvPath $uvPath `
            -Arguments $arguments `
            -WheelPath $wheelPath `
            -Running $running `
            -Version $release.Version
    }

    try {
        Invoke-NativeCommand -FilePath $uvPath -Arguments $arguments
    }
    finally {
        Remove-Item -LiteralPath (Split-Path -Parent $wheelPath) -Recurse -Force -ErrorAction SilentlyContinue
    }
    return $release.Version
}

function Configure-AndConfirmFreeClaudeCode {
    param([Parameter(Mandatory = $true)] [string] $ExpectedVersion)

    if ($DryRun) {
        Write-Host "+ uv tool update-shell"
        Write-Host "+ uv tool dir --bin"
        Write-Host "+ verify mcc-server, mcc-claude, mcc-codex, mcc-pi, mcc-help, and my-claude-code in the uv tool bin directory"
        Write-Host "+ mcc-server --version"
        return
    }

    $uvCommand = Get-ApplicationCommand "uv"
    if (-not $uvCommand) {
        throw "uv is not available for PATH configuration."
    }
    Invoke-NativeCommand -FilePath $uvCommand.Source -Arguments @("tool", "update-shell")
    $toolBin = Invoke-NativeCapture -FilePath $uvCommand.Source -Arguments @("tool", "dir", "--bin")
    if ([string]::IsNullOrWhiteSpace($toolBin)) {
        throw "uv returned an empty tool bin directory."
    }

    Add-PathEntry $toolBin
    $toolBinPath = ([IO.Path]::GetFullPath($toolBin)).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    # Verify the native my-claude-code command family (mcc-*) plus the package
    # name shim, exactly as the post-install reference on WSL/Linux leads with.
    # The legacy fcc-* aliases resolve through the same distribution, so they
    # exist as soon as these do.
    $mccCommands = @(
        "mcc-server", "mcc-claude", "mcc-claude-old", "mcc-codex", "mcc-pi",
        "mcc-init", "mcc-chatgpt-oauth-login", "mcc-compact-log", "mcc-help",
        "my-claude-code"
    )
    $installedCommands = @{}
    foreach ($commandName in $mccCommands) {
        $command = Get-ApplicationCommand $commandName
        if (-not $command) {
            throw "My Claude Code installation did not create '$commandName'."
        }
        $commandDirectory = ([IO.Path]::GetFullPath((Split-Path -Parent $command.Source))).TrimEnd(
            [IO.Path]::DirectorySeparatorChar,
            [IO.Path]::AltDirectorySeparatorChar
        )
        if (-not $commandDirectory.Equals($toolBinPath, [StringComparison]::OrdinalIgnoreCase)) {
            throw "'$commandName' resolved outside the uv tool bin directory: $($command.Source)"
        }
        $installedCommands[$commandName] = $command.Source
    }

    $installedVersion = Invoke-NativeCapture -FilePath $installedCommands["mcc-server"] -Arguments @("--version")
    if ($installedVersion -ne "my-claude-code $ExpectedVersion") {
        throw "Expected my-claude-code $ExpectedVersion; found: $installedVersion"
    }
}

function Write-MccCommandReference {
    # Shown after a successful install (direct or deferred) so the user sees the
    # same command reference on Windows as on Linux/WSL.
    Write-Host ""
    Write-Host "Start the proxy:"
    Write-Host "  mcc-server              Start the local proxy and admin dashboard"
    Write-Host ""
    Write-Host "Use a coding agent through the proxy:"
    Write-Host "  mcc-claude              Launch Claude Code through the proxy"
    Write-Host "  mcc-claude --discover-models   Enable the model picker from the catalog"
    Write-Host "  mcc-codex               Launch Codex through the proxy"
    Write-Host "  mcc-pi                  Launch Pi through the proxy"
    Write-Host ""
    Write-Host "Manage and inspect:"
    Write-Host "  mcc-init                Create or repair ~/.fcc/.env"
    Write-Host "  mcc-help                Show what each command does"
    Write-Host ""
    Write-Host "The legacy fcc-* commands (fcc-server, fcc-claude, ...) remain as aliases."
    Write-Host ""
    Write-Host "To use an update installed while the server is running, restart the proxy"
    Write-Host "with: mcc-server"
}

function Get-LauncherCommands {
    # Both command families share the tool bin directory, so any of them holds
    # the shim uv must replace.
    return @(
        "fcc-server", "fcc-claude", "fcc-claude-old", "fcc-codex", "fcc-pi",
        "fcc-init", "fcc-chatgpt-oauth-login", "fcc-compact-log",
        "free-claude-code",
        "mcc-server", "mcc-claude", "mcc-claude-old", "mcc-codex", "mcc-pi",
        "mcc-init", "mcc-chatgpt-oauth-login", "mcc-compact-log",
        "my-claude-code"
    )
}

function Get-RunningLaunchers {
    # Return the process objects of any launcher currently running. These are the
    # processes whose shims uv must replace, so an install cannot proceed while
    # they live. We defer rather than refuse: the update completes after the app
    # is restarted, exactly as on POSIX.
    $running = @()
    foreach ($commandName in Get-LauncherCommands) {
        $processes = @(Get-Process -Name $commandName -ErrorAction SilentlyContinue)
        foreach ($process in $processes) {
            $running += $process
        }
    }
    # Emit to the pipeline (no explicit return) so the caller's @(...) captures
    # 0, 1, or many results as an array. A bare return of $running would unwrap
    # a single Process object into a scalar and a later `.Count` would fail
    # under Set-StrictMode.
    foreach ($process in $running) {
        $process
    }
}

function Start-DeferredInstall {
    param(
        [Parameter(Mandatory = $true)] [string] $UvPath,
        [Parameter(Mandatory = $true)] [string[]] $Arguments,
        [Parameter(Mandatory = $true)] [string] $WheelPath,
        [Parameter(Mandatory = $true)] [object[]] $Running,
        [Parameter(Mandatory = $true)] [string] $Version
    )

    # Keep the verified wheel where a detached helper can reach it. The wheel
    # directory must survive this process exiting, so stage under TEMP, not a
    # tempfile that is deleted on scope exit.
    $stageDir = Join-Path ([IO.Path]::GetTempPath()) ("mcc-deferred-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $stageDir | Out-Null
    $stagedWheel = Join-Path $stageDir (Split-Path -Leaf $WheelPath)
    Copy-Item -LiteralPath $WheelPath -Destination $stagedWheel -Force
    Remove-Item -LiteralPath (Split-Path -Parent $WheelPath) -Recurse -Force -ErrorAction SilentlyContinue

    # The last argument is the package spec ("my-claude-code[...] @ file:///...").
    # Point it at the staged wheel so the detached helper installs the verified
    # artifact, not the temp copy we just deleted. Preserve any extras prefix
    # (voice / voice_local) by reusing the part before " @ ".
    $stagedUrl = ([Uri]::new($stagedWheel)).AbsoluteUri
    if ($Arguments.Count -gt 0) {
        $last = $Arguments[-1]
        $packagePrefix = ($last -split " @ ", 2)[0]
        $stagedSpec = "$packagePrefix @ $stagedUrl"
        $Arguments = $Arguments[0..($Arguments.Count - 2)] + $stagedSpec
    }

    # Wait for every running launcher to exit (bounded), then install the staged
    # wheel. The user restarts the app themselves; we must NOT start it, or we
    # would replace the same processes we waited for. Retry the install with
    # backoff because handle release is not instantaneous on Windows.
    $pidsLiteral = ($Running | ForEach-Object {
        "'" + ($_.Id.ToString() -replace "'", "''") + "'"
    }) -join ", "
    $uvLiteral = "'" + ($UvPath -replace "'", "''") + "'"
    $stageDirLiteral = "'" + ($stageDir -replace "'", "''") + "'"
    $argumentsLiteral = "& $uvLiteral @(" + (($Arguments | ForEach-Object {
        "'" + ($_ -replace "'", "''") + "'"
    }) -join ", ") + ")"

    $script = @"
`$ErrorActionPreference = 'Stop'
`$deadline = (Get-Date).AddHours(6)
`$pids = @($pidsLiteral)
while ((Get-Date) -lt `$deadline) {
    `$alive = `$pids | Where-Object { Get-Process -Id `$_ -ErrorAction SilentlyContinue }
    if (-not `$alive) { break }
    Start-Sleep -Milliseconds 500
}
if (`$pids | Where-Object { Get-Process -Id `$_ -ErrorAction SilentlyContinue }) {
    Write-Host "My Claude Code did not stop within 6 hours; install not applied."
    exit 1
}
Start-Sleep -Seconds 2
`$ErrorActionPreference = 'Continue'
`$delays = @(0, 5, 10, 20, 30)
`$ok = `$false
foreach (`$wait in `$delays) {
    if (`$wait -gt 0) { Start-Sleep -Seconds `$wait }
    $argumentsLiteral 2>&1 | Out-String | Out-Null
    if (`$LASTEXITCODE -eq 0) { `$ok = `$true; break }
}
`$ErrorActionPreference = 'Stop'
if (`$ok) {
    Remove-Item -Path $stageDirLiteral -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "My Claude Code install completed. Start the app with: mcc-server"
}
else {
    Write-Host "My Claude Code install failed after multiple attempts. Re-run the installer."
}
"@

    $helperPath = Join-Path $stageDir "apply-update.ps1"
    Set-Content -LiteralPath $helperPath -Value $script -Encoding UTF8

    # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP, same as the dashboard updater:
    # the child needs a console to run but must not be signalled when the console
    # this installer was launched from closes.
    $flags = 0x08000000 -bor 0x00000200
    $process = Start-Process `
        -FilePath "powershell.exe" `
        -ArgumentList @("-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", $helperPath) `
        -WindowStyle Hidden `
        -PassThru
    $null = $process

    $script:Deferred = $true

    Write-Host "My Claude Code is currently running. The update to v$Version is staged and"
    Write-Host "will complete after you stop the running app, then restart it (mcc-server)."
    Write-Host "The new version is picked up on restart."
    Write-MccCommandReference
    return $Version
}

if ($Help) {
    Show-Usage
    return
}

if ($RemainingArgs.Count -gt 0) {
    Show-Usage
    throw "Unknown option: $($RemainingArgs -join ' ')"
}

if ((-not [string]::IsNullOrWhiteSpace($TorchBackend)) -and (-not ($VoiceLocal -or $VoiceAll))) {
    throw "-TorchBackend requires -VoiceLocal or -VoiceAll."
}

Add-KnownBinDirectories

Write-Step "Ensuring uv $MinUvVersion or newer is installed"
Ensure-Uv

Write-Step "Installing or updating My Claude Code"
$InstalledVersion = Install-FreeClaudeCode

if ($script:Deferred) {
    Write-Host ""
    Write-Host "Update staged for after restart."
}
elseif ($DryRun) {
    Write-Host ""
    Write-Host "Dry run complete. No changes were made."
}
else {
    Write-Step "Configuring PATH and verifying My Claude Code"
    Configure-AndConfirmFreeClaudeCode -ExpectedVersion $InstalledVersion

    Write-Host ""
    Write-Host "My Claude Code $InstalledVersion is installed and verified."
    Write-MccCommandReference
}
