# NSE MT5 Bridge — direct GitHub Releases installer.
#
# Installs (or upgrades) the Bridge from the official GitHub Releases of
# Opselon/NSE-Connector-Bridge. No Python, no Node, no package manager.
#
# One-line bootstrap (from any PowerShell):
#   $s = "$env:TEMP\nse-bridge-install.ps1"
#   Invoke-WebRequest "https://raw.githubusercontent.com/Opselon/NSE-Connector-Bridge/main/scripts/install.ps1" -OutFile $s
#   & $s -Channel Stable
#
# Options:
#   -Channel    Stable (default) | Prerelease
#   -Version    Install a specific release version, e.g. 1.2.0 or v1.2.0
#               (overrides -Channel)
#   -InstallDir Target directory (default: the per-user install location
#               %LOCALAPPDATA%\Programs\NSE MT5 Bridge, matching the Inno
#               Setup default — an existing install is upgraded in place)
#   -Portable   Install the standalone exe instead of running the setup
#               installer (no Start Menu entry, no uninstaller)
#   -Silent     Run the Inno Setup installer with /SILENT (no wizard UI)
#   -Force      Reinstall even when the requested version is already
#               installed
#   -VerifyOnly Download + verify everything, install nothing
#
# What is verified, honestly:
#   - SHA-256 of every downloaded artifact against BOTH the release
#     manifest (release-manifest.json) and the .sha256 sidecar — the two
#     must agree with each other and with the file on disk.
#   - Ed25519 release signatures: the Platform signs releases per
#     CONTRACT v1 §3.5/§12.4, but no signing key is published yet
#     (releases/latest answers signature: null). Until a signed
#     manifest exists this script has nothing to verify a signature
#     against, so it reports that dependency explicitly instead of
#     pretending the check ran. It NEVER fabricates a key and NEVER
#     bypasses verification to make an install succeed.
#
# Integrity model: the manifest and sidecars travel with the release
# assets over HTTPS from github.com/Opselon. They protect against
# corruption and mixed-up artifacts, not against a compromised release
# pipeline — that is what Ed25519 signatures are reserved for (see
# docs/SECURITY.md). This limitation is reported at runtime.
#
# Configuration/credentials are never touched: config.json and the OS
# secret store live under %LOCALAPPDATA%\NSEBridge, outside any
# install directory, so install/upgrade/uninstall preserve them.
#
# Exit codes:
#   0  success (or verified, with -VerifyOnly)
#   1  script usage error
#   2  unsupported Windows version or CPU architecture
#   3  could not reach GitHub / metadata unusable
#   4  expected release artifact missing from the release
#   5  checksum mismatch (manifest vs sidecar vs file)
#   6  installer failed
#   7  post-install verification failed
#
# Idempotent: safe to rerun; reruns re-download and re-verify, and the
# Inno Setup installer upgrades in place (same AppId GUID).

[CmdletBinding()]
param(
    [ValidateSet("Stable", "Prerelease")]
    [string]$Channel = "Stable",

    [string]$Version = "",

    [string]$InstallDir = "",

    [switch]$Portable,

    [switch]$Silent,

    [switch]$Force,

    [switch]$VerifyOnly
)

$ErrorActionPreference = "Stop"
$RepoOwner = "Opselon"
$RepoName = "NSE-Connector-Bridge"
$ExeName = "NSE-MT5-Bridge.exe"
$DefaultInstallDir = Join-Path $env:LOCALAPPDATA "Programs\NSE MT5 Bridge"

# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

function Write-Step([string]$Message) { Write-Host "==> $Message" }
function Write-Warn2([string]$Message) { Write-Host "WARNING: $Message" -ForegroundColor Yellow }

function Invoke-GitHubApi([string]$Url) {
    # GitHub API with a browser-grade UA (api.github.com rejects
    # default PowerShell UA), TLS 1.2+ for older hosts.
    $headers = @{ "User-Agent" = "nse-bridge-installer/1.0"; "Accept" = "application/vnd.github+json" }
    try {
        return Invoke-RestMethod -Uri $Url -Headers $headers -Method Get
    } catch {
        throw "GitHub API request failed ($Url): $($_.Exception.Message)"
    }
}

function Get-ReleaseForChannel([string]$RequestedVersion, [string]$ChannelName) {
    if ($RequestedVersion) {
        $tag = if ($RequestedVersion.StartsWith("v")) { $RequestedVersion } else { "v$RequestedVersion" }
        Write-Step "Resolving release $tag"
        return Invoke-GitHubApi "https://api.github.com/repos/$RepoOwner/$RepoName/releases/tags/$tag"
    }
    if ($ChannelName -eq "Stable") {
        Write-Step "Resolving latest stable release"
        return Invoke-GitHubApi "https://api.github.com/repos/$RepoOwner/$RepoName/releases/latest"
    }
    # Prerelease channel: newest release of any kind (the API returns
    # newest first; /releases/latest would hide prereleases).
    Write-Step "Resolving newest release (prereleases included)"
    $list = Invoke-GitHubApi "https://api.github.com/repos/$RepoOwner/$RepoName/releases?per_page=10"
    if (-not $list -or $list.Count -eq 0) { throw "No releases published in $RepoOwner/$RepoName" }
    return $list[0]
}

function Get-ReleaseAsset($Release, [string]$Name) {
    $asset = @($Release.assets) | Where-Object { $_.name -eq $Name } | Select-Object -First 1
    if (-not $asset) {
        throw [System.InvalidOperationException]::new(
            "Release $($Release.tag_name) has no asset named '$Name'. Available: " +
            ((@($Release.assets) | ForEach-Object { $_.name }) -join ", "))
    }
    return $asset
}

function Get-FileSha256([string]$Path) {
    return (Get-FileHash -Path $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-ChecksumConsistent([string]$FilePath, [string]$ManifestJson, [string]$SidecarPath) {
    # Three sources must agree: the file on disk, the release manifest,
    # and the .sha256 sidecar. Any disagreement = corruption or a mixed
    # set of artifacts; fail closed (exit code 5).
    $actual = Get-FileSha256 $FilePath
    $fileName = Split-Path $FilePath -Leaf

    $manifestEntry = $ManifestJson.files | Where-Object { $_.name -eq $fileName } | Select-Object -First 1
    if (-not $manifestEntry) {
        throw "release-manifest.json does not list '$fileName' — refusing to trust an unmanifested artifact"
    }
    if ($manifestEntry.sha256.ToLowerInvariant() -ne $actual) {
        throw "SHA-256 MISMATCH for '$fileName' (manifest $($manifestEntry.sha256) != actual $actual)"
    }

    if ($SidecarPath) {
        $sidecarLine = (Get-Content $SidecarPath -TotalCount 1).Trim()
        # Sidecar format: "<sha256>  <filename>" (two spaces, sha256 first).
        $sidecarSha = ($sidecarLine -split '\s+')[0].ToLowerInvariant()
        if ($sidecarSha -ne $actual) {
            throw "SHA-256 MISMATCH for '$fileName' (sidecar $sidecarSha != actual $actual)"
        }
        if ($manifestEntry.sha256.ToLowerInvariant() -ne $sidecarSha) {
            throw "release-manifest.json and the .sha256 sidecar disagree for '$fileName'"
        }
    }
    return $actual
}

# ---------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------

if ($PSVersionTable.PSVersion.Major -lt 5) {
    Write-Host "error: Windows PowerShell 5.1 or PowerShell 7+ is required" -ForegroundColor Red
    exit 1
}

Write-Step "Checking environment"
$is64 = [Environment]::Is64BitOperatingSystem
$osVersion = [Environment]::OSVersion.Version
if (-not $is64) {
    Write-Host "error: unsupported architecture — the Bridge ships Windows x64 only (see docs/COMPATIBILITY.md)" -ForegroundColor Red
    exit 2
}
if ($osVersion.Major -lt 10) {
    Write-Host "error: unsupported Windows version $($osVersion) — Windows 10 or newer is required" -ForegroundColor Red
    exit 2
}
Write-Host ("    Windows {0} x64 (PowerShell {1})" -f $osVersion.ToString(3), $PSVersionTable.PSVersion.ToString())
# Explicit TLS 1.2 for Windows PowerShell 5.1 on older stacks (no-op on 7+).
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072 } catch { }

# ---------------------------------------------------------------------
# Resolve the release
# ---------------------------------------------------------------------

$release = Get-ReleaseForChannel $Version $Channel
$releaseVersion = $release.tag_name.TrimStart("v")
Write-Host ("    resolved: {0} (published {1}, prerelease: {2})" -f $release.tag_name, $release.published_at, $release.prerelease)

# ---------------------------------------------------------------------
# Download + verify
# ---------------------------------------------------------------------

$tempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("nse-bridge-install-" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Path $tempDir | Out-Null
$headers = @{ "User-Agent" = "nse-bridge-installer/1.0" }

try {
    $manifestAsset = Get-ReleaseAsset $release "release-manifest.json"
    $manifestPath = Join-Path $tempDir "release-manifest.json"
    Write-Step "Downloading release-manifest.json"
    Invoke-WebRequest -Uri $manifestAsset.browser_download_url -Headers $headers -OutFile $manifestPath
    $manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json
    Write-Host ("    manifest version: {0} (platform {1})" -f $manifest.version, $manifest.platform)
    if ($manifest.platform -ne "windows-x64") {
        Write-Host "error: release manifest targets '$($manifest.platform)' — this installer requires windows-x64" -ForegroundColor Red
        exit 2
    }
    if ($manifest.version -ne $releaseVersion) {
        throw "manifest version $($manifest.version) does not match release tag $($release.tag_name)"
    }

    $wanted = if ($Portable) { $ExeName } else { "NSE-MT5-Bridge-Setup-$releaseVersion.exe" }
    Write-Step "Downloading $wanted"
    $artifactAsset = Get-ReleaseAsset $release $wanted
    $artifactPath = Join-Path $tempDir $wanted
    Invoke-WebRequest -Uri $artifactAsset.browser_download_url -Headers $headers -OutFile $artifactPath

    Write-Step "Verifying SHA-256 (manifest + sidecar + file)"
    $sidecarAsset = Get-ReleaseAsset $release "$wanted.sha256"
    $sidecarPath = Join-Path $tempDir "$wanted.sha256"
    Invoke-WebRequest -Uri $sidecarAsset.browser_download_url -Headers $headers -OutFile $sidecarPath
    $verified = Assert-ChecksumConsistent $artifactPath $manifest $sidecarPath
    Write-Host ("    verified {0} ({1} bytes, sha256 {2}...)" -f $wanted, $artifactAsset.size, $verified.Substring(0, 16))

    # Signature provenance — report honestly, never fake a pass.
    # Signed releases (P1-6) carry a base64 Ed25519 signature per
    # file in the manifest ("signature" on the files[] entry) plus a
    # "<name>.sig" sidecar on the release. This installer cannot
    # verify Ed25519 natively (no Ed25519 primitive in Windows
    # PowerShell / .NET), so a present signature is REPORTED, not
    # verified here — verify it with
    #   python scripts/sign_release_artifact.py --verify
    #       --public-key-file nse_bridge/update_public_key.b64
    #       --manifest release-manifest.json <artifact>
    # before installing. The SHA-256 chain above is the verification
    # this script itself performs. An unsigned release (no signature
    # in the manifest, e.g. v1.4.0) still says so explicitly.
    $manifestEntry = $manifest.files | Where-Object { $_.name -eq $wanted } | Select-Object -First 1
    if ($manifestEntry -and $manifestEntry.signature) {
        Write-Host ("    Ed25519 signature published for {0} (base64 {1}...) — not verified by this installer (no Ed25519 primitive in PowerShell); verify with scripts/sign_release_artifact.py --verify before installing." -f $wanted, $manifestEntry.signature.Substring(0, 16))
    } else {
        Write-Warn2 "No Ed25519 release signature is published for this release yet (CONTRACT v1 §3.5 reserves one; the signing pipeline is pending). Verification relied on the SHA-256 manifest + sidecar over HTTPS."
    }

    if ($VerifyOnly) {
        Write-Step "Verify-only requested — download and verification complete, nothing installed"
        exit 0
    }

    # -----------------------------------------------------------------
    # Detect existing installation
    # -----------------------------------------------------------------

    $existingExe = if ($InstallDir) { Join-Path $InstallDir $ExeName } else { Join-Path $DefaultInstallDir $ExeName }
    $alreadyInstalled = Test-Path $existingExe
    if ($alreadyInstalled) {
        $installedVersion = (Get-Item $existingExe).VersionInfo.ProductVersion
        Write-Host ("    existing installation found: {0} (version {1})" -f $existingExe, $installedVersion)
        if (-not $Force -and $installedVersion -eq $releaseVersion) {
            Write-Step "Version $releaseVersion is already installed — nothing to do (use -Force to reinstall)"
            exit 0
        }
        Write-Step "Upgrading in place (configuration and credentials are preserved)"
    } else {
        Write-Step "No existing installation found — fresh install"
    }

    # -----------------------------------------------------------------
    # Install
    # ---------------------------------------------------------------------

    if ($Portable) {
        $targetDir = if ($InstallDir) { $InstallDir } else { $DefaultInstallDir }
        New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
        Copy-Item $artifactPath (Join-Path $targetDir $ExeName) -Force
    } else {
        $setupArgs = @()
        if ($InstallDir) { $setupArgs += "/DIR=`"$InstallDir`"" }
        if ($Silent) { $setupArgs += "/SILENT" }
        Write-Step "Running the installer$($(if ($setupArgs) { ' (' + ($setupArgs -join ' ') + ')' } else { '' }))"
        $proc = Start-Process -FilePath $artifactPath -ArgumentList $setupArgs -Wait -PassThru
        if ($proc.ExitCode -ne 0) {
            Write-Host ("error: installer exited with code {0}" -f $proc.ExitCode) -ForegroundColor Red
            exit 6
        }
        $targetDir = if ($InstallDir) { $InstallDir } else { $DefaultInstallDir }
    }

    # -----------------------------------------------------------------
    # Post-install verification
    # ---------------------------------------------------------------------

    $installedExe = Join-Path $targetDir $ExeName
    if (-not (Test-Path $installedExe)) {
        Write-Host "error: $installedExe not found after installation" -ForegroundColor Red
        exit 7
    }
    Write-Step "Verifying the installed executable"
    $installedSha = Get-FileSha256 $installedExe
    if ($installedSha -ne $verified) {
        Write-Host ("error: installed exe sha256 {0} does not match the verified download {1}" -f $installedSha, $verified) -ForegroundColor Red
        exit 7
    }
    $installedVersion = (Get-Item $installedExe).VersionInfo.ProductVersion

    Write-Host ""
    Write-Host ("NSE MT5 Bridge {0} installed at {1}" -f $releaseVersion, $targetDir) -ForegroundColor Green
    Write-Host ("    installed exe sha256 verified: {0}..." -f $installedSha.Substring(0, 16))
    Write-Host "    Start it from the Start Menu ('NSE MT5 Bridge') or run: $installedExe"
    Write-Host "    Machine-readable status (once running): $installedExe status --json"
    exit 0
} catch {
    $code = 3
    $msg = $_.Exception.Message
    if ($msg -match "no asset named") { $code = 4 }
    elseif ($msg -match "MISMATCH|does not list|disagree") { $code = 5 }
    Write-Host ("error: {0}" -f $msg) -ForegroundColor Red
    Write-Host "Recovery: rerun this script (it is idempotent); use -Version to pin a release, -VerifyOnly to test the download path, or download + verify the artifacts manually from https://github.com/$RepoOwner/$RepoName/releases"
    exit $code
} finally {
    if (Test-Path $tempDir) {
        Remove-Item $tempDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
