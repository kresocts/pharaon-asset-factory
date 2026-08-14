Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Test-PublisherReleaseTag {
    param(
        [AllowEmptyString()]
        [string]$ReleaseTag
    )

    if ($ReleaseTag -eq "") {
        return $true
    }

    if ($ReleaseTag.Length -gt 128) {
        return $false
    }
    if ($ReleaseTag -match "\s") {
        return $false
    }
    if ($ReleaseTag.Contains("/")) {
        return $false
    }
    if ($ReleaseTag.ToLowerInvariant() -cne $ReleaseTag) {
        return $false
    }
    if ($ReleaseTag -match '[&|;<>$]') {
        return $false
    }
    if (-not ($ReleaseTag -cmatch "^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-(?:[0-9a-z]+(?:-[0-9a-z]+)*)(?:\.(?:[0-9a-z]+(?:-[0-9a-z]+)*))*)?$")) {
        return $false
    }

    return $true
}

function Assert-PublisherReleaseTag {
    param(
        [AllowEmptyString()]
        [string]$ReleaseTag
    )

    if (-not (Test-PublisherReleaseTag -ReleaseTag $ReleaseTag)) {
        throw "release_tag must be an immutable version such as v1.0.0 or v1.2.3-rc.1."
    }
}

function Assert-PublisherShaTag {
    param(
        [string]$Sha
    )

    if ($Sha -notmatch "^[0-9a-f]{40}$") {
        throw "The immutable SHA tag requires a full 40-character lowercase hex SHA."
    }
}

function Get-PublisherTags {
    param(
        [string]$Image,
        [string]$Sha,
        [AllowEmptyString()]
        [string]$ReleaseTag
    )

    Assert-PublisherShaTag -Sha $Sha
    Assert-PublisherReleaseTag -ReleaseTag $ReleaseTag

    $tags = @("${Image}:sha-${Sha}")
    if ($ReleaseTag -ne "") {
        $tags += "${Image}:${ReleaseTag}"
    }
    return $tags
}

function Test-PublisherRegistryTagState {
    param(
        [string]$DockerConfig,
        [string]$Reference,
        [switch]$Insecure
    )

    if (-not (Test-Path -LiteralPath $DockerConfig)) {
        throw "Temporary Docker config path does not exist: $DockerConfig"
    }

    $dockerArgs = @(
        "--config"
        $DockerConfig
        "manifest"
        "inspect"
    )
    if ($Insecure) {
        $dockerArgs += "--insecure"
    }
    $dockerArgs += $Reference
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & docker @dockerArgs 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
    $text = (($output | ForEach-Object { $_.ToString() }) -join "`n")

    if ($exitCode -eq 0) {
        return "Existing"
    }

    $absenceSignals = @(
        "MANIFEST_UNKNOWN"
        "NAME_UNKNOWN"
        "manifest unknown"
        "no such manifest"
    )
    foreach ($signal in $absenceSignals) {
        if ($text -match [regex]::Escape($signal)) {
            return "Absent"
        }
    }

    return "Error"
}

function Assert-PublisherTagsAbsent {
    param(
        [string]$DockerConfig,
        [string]$Image,
        [string[]]$Tags,
        [switch]$Insecure
    )

    foreach ($tag in $Tags) {
        $reference = "${Image}:${tag}"
        $state = Test-PublisherRegistryTagState -DockerConfig $DockerConfig -Reference $reference -Insecure:$Insecure
        if ($state -eq "Existing") {
            throw "Requested tag already exists and publication is refused: $tag"
        }
        if ($state -ne "Absent") {
            throw "Registry or authentication error while checking tag $tag. Refusing to continue."
        }
        Write-Host "Requested tag is not present: $tag"
    }
}

function New-PublisherTemporaryDockerConfig {
    param(
        [string]$BasePath,
        [string]$Name
    )

    if (-not (Test-Path -LiteralPath $BasePath)) {
        throw "Temporary Docker config base path does not exist: $BasePath"
    }

    $configPath = Join-Path $BasePath $Name
    if (Test-Path -LiteralPath $configPath) {
        throw "Temporary Docker config already exists: $configPath"
    }

    New-Item -ItemType Directory -Path $configPath | Out-Null
    return $configPath
}

function Invoke-PublisherBuildxBuild {
    param(
        [string]$DockerConfig,
        [string]$Dockerfile,
        [string]$Platform,
        [string]$Context,
        [string]$Image,
        [string]$Sha,
        [AllowEmptyString()]
        [string]$ReleaseTag,
        [hashtable]$Labels,
        [string]$MetadataFile
    )

    if (-not (Test-Path -LiteralPath $DockerConfig)) {
        throw "Temporary Docker config path does not exist: $DockerConfig"
    }

    Assert-PublisherShaTag -Sha $Sha
    Assert-PublisherReleaseTag -ReleaseTag $ReleaseTag

    $tags = @("${Image}:sha-${Sha}")
    if ($ReleaseTag -ne "") {
        $tags += "${Image}:${ReleaseTag}"
    }

    $buildArgs = @(
        "--file"
        $Dockerfile
        "--platform"
        $Platform
        "--provenance=false"
        "--sbom=false"
        "--metadata-file"
        $MetadataFile
        "--push"
    )

    foreach ($tag in $tags) {
        $buildArgs += "--tag"
        $buildArgs += $tag
    }

    foreach ($entry in ($Labels.GetEnumerator() | Sort-Object Name)) {
        $buildArgs += "--label"
        $buildArgs += "$($entry.Key)=$($entry.Value)"
    }

    $buildArgs += $Context

    $dockerArgs = @(
        "--config"
        $DockerConfig
        "buildx"
        "build"
    ) + $buildArgs

    & docker @dockerArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Buildx push failed."
    }
}

function Read-PublisherBuildMetadataDigest {
    param(
        [string]$MetadataFile
    )

    if (-not (Test-Path -LiteralPath $MetadataFile)) {
        throw "Buildx metadata file was not produced."
    }

    try {
        $metadata = Get-Content -LiteralPath $MetadataFile -Raw | ConvertFrom-Json
    }
    catch {
        throw "Buildx metadata file is malformed."
    }

    $digest = $metadata."containerimage.digest"
    if (-not $digest -or $digest -notmatch "^sha256:[0-9a-f]{64}$") {
        throw "Buildx metadata did not contain a valid sha256 digest."
    }

    return $digest
}

function Get-PublisherImageInspection {
    param(
        [string]$DockerConfig,
        [string]$Reference
    )

    $dockerArgs = @(
        "--config"
        $DockerConfig
        "buildx"
        "imagetools"
        "inspect"
        $Reference
        "--format"
        "{{json .}}"
    )

    $output = & docker @dockerArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Buildx imagetools inspect failed for $Reference."
    }

    $json = (($output | ForEach-Object { $_.ToString() }) -join "`n").Trim()
    if (-not $json) {
        throw "Buildx imagetools inspect returned no output for $Reference."
    }

    try {
        return ($json | ConvertFrom-Json)
    }
    catch {
        throw "Buildx imagetools inspect returned malformed JSON for $Reference."
    }
}

function Assert-PublisherLinuxAmd64 {
    param(
        $Inspection,
        [string]$Label
    )

    $verified = $false

    if ($null -ne $Inspection.manifest -and $Inspection.manifest.PSObject.Properties.Name -contains "manifests") {
        $platformManifests = @($Inspection.manifest.manifests)
        if ($platformManifests.Count -ne 1) {
            throw "$Label does not contain exactly one Linux AMD64 platform manifest."
        }
        $platform = $platformManifests[0].platform
        if ($null -eq $platform -or $platform.os -ne "linux" -or $platform.architecture -ne "amd64") {
            throw "$Label is not verified as Linux AMD64 from manifest platforms."
        }
        $verified = $true
    }

    if ($null -ne $Inspection.image) {
        $imageOs = $Inspection.image.os
        $imageArch = $Inspection.image.architecture
        if ($null -ne $imageOs -and $null -ne $imageArch) {
            if ($imageOs -ne "linux" -or $imageArch -ne "amd64") {
                throw "$Label is not verified as Linux AMD64 from image config."
            }
            $verified = $true
        }
    }

    if (-not $verified) {
        throw "$Label could not be verified as Linux AMD64 from supported inspection output."
    }
}

function Assert-PublisherPushedDigest {
    param(
        [string]$DockerConfig,
        [string]$Image,
        [string]$Sha,
        [AllowEmptyString()]
        [string]$ReleaseTag,
        [string]$Digest
    )

    $shaTag = "sha-$Sha"
    $shaReference = "${Image}:${shaTag}"
    $shaInspection = Get-PublisherImageInspection -DockerConfig $DockerConfig -Reference $shaReference
    $shaManifestDigest = $shaInspection.manifest.digest
    if (-not $shaManifestDigest -or $shaManifestDigest -ne $Digest) {
        throw "Pushed SHA tag does not resolve to the captured digest."
    }
    Assert-PublisherLinuxAmd64 -Inspection $shaInspection -Label "SHA tag"

    if ($ReleaseTag -ne "") {
        $releaseReference = "${Image}:${ReleaseTag}"
        $releaseInspection = Get-PublisherImageInspection -DockerConfig $DockerConfig -Reference $releaseReference
        $releaseManifestDigest = $releaseInspection.manifest.digest
        if (-not $releaseManifestDigest -or $releaseManifestDigest -ne $Digest) {
            throw "Optional release tag does not resolve to the captured digest."
        }
        Assert-PublisherLinuxAmd64 -Inspection $releaseInspection -Label "Release tag"
    }
}

function Get-PublisherDigestQualifiedReference {
    param(
        [string]$Image,
        [string]$Digest
    )

    return "${Image}@${Digest}"
}

function Get-PublisherProcessExitCode {
    param(
        [bool]$PrimarySuccess,
        [bool]$CleanupSuccess,
        [bool]$EvidenceWritten
    )

    if (-not $PrimarySuccess -or -not $CleanupSuccess -or -not $EvidenceWritten) {
        return 1
    }
    return 0
}
