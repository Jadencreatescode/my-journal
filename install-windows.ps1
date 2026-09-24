[CmdletBinding(DefaultParameterSetName = "Install")]
param(
    [Parameter()]
    [string]$HermesHome = (Join-Path $env:LOCALAPPDATA "hermes"),

    [Parameter()]
    [ValidatePattern('^[A-Za-z0-9._-]{1,64}$')]
    [string]$Distro = "Ubuntu",

    [Parameter(ParameterSetName = "Install")]
    [switch]$Upgrade,

    [Parameter(Mandatory, ParameterSetName = "Restore")]
    [switch]$Restore,

    [Parameter(Mandatory, ParameterSetName = "Uninstall")]
    [switch]$Uninstall,

    [Parameter()]
    [switch]$Force
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Convert-ToWslPath {
    param([Parameter(Mandatory)][string]$WindowsPath)

    $fullPath = [System.IO.Path]::GetFullPath($WindowsPath)
    $rootPath = [System.IO.Path]::GetPathRoot($fullPath)
    if ($rootPath -notmatch '^([A-Za-z]):\\$') {
        throw "The Windows Journal bridge accepts only an absolute local Windows drive path."
    }
    $drive = $Matches[1].ToLowerInvariant()
    $relativePath = $fullPath.Substring($rootPath.Length).Replace('\', '/')
    if ([string]::IsNullOrEmpty($relativePath)) {
        return "/mnt/$drive"
    }
    return "/mnt/$drive/$relativePath"
}

$SourceRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Installer = Join-Path $SourceRoot "install.py"
if (-not (Test-Path -LiteralPath $Installer -PathType Leaf)) {
    throw "The My Journal installer is missing from the candidate package."
}

& wsl.exe -d $Distro -- python3 --version | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Ubuntu WSL with Python 3 is required for the restricted Journal runtime."
}

$WslSource = Convert-ToWslPath -WindowsPath $SourceRoot
$WslHome = Convert-ToWslPath -WindowsPath ([System.IO.Path]::GetFullPath($HermesHome))
$Arguments = @(
    "-d", $Distro, "--", "python3", "$WslSource/install.py",
    "--hermes-home", $WslHome
)

if ($Upgrade) {
    $Arguments += "--upgrade"
}
if ($Restore) {
    $Arguments += "--restore"
}
if ($Uninstall) {
    $Arguments += "--uninstall"
}
if ($Force) {
    $Arguments += "--force"
}

& wsl.exe @Arguments
if ($LASTEXITCODE -ne 0) {
    throw "The transactional My Journal installer failed with exit code $LASTEXITCODE."
}

Write-Output "My Journal Windows bridge installation completed for $HermesHome"
