param(
    [switch]$PreflightOnly,
    [string]$Confirmation = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepositoryRoot

$PublisherScript = Join-Path $RepositoryRoot "scripts/publisher/publisher.ps1"
. $PublisherScript

$RegistryImage = "registry:2.8.3@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373"
$RegistryContainerBase = "pharaon-t0016-registry"
$ReleaseTag = "v0.0.0-rc.1"
$RecordPath = Join-Path $RepositoryRoot "validation/records/T-0016-local-publisher-validation.md"
$Dockerfile = "docker/Dockerfile"
$Platform = "linux/amd64"
$RequiredFreeGiB = [int64]150

function Get-AvailableLoopbackPort {
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
    try {
        $listener.Start()
        $port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
        if ($port -lt 1024) {
            throw "No suitable high loopback port was available."
        }
        return $port
    }
    finally {
        $listener.Stop()
    }
}

function Get-DockerConfigFingerprint {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return "ABSENT"
    }

    $hash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
    $item = Get-Item -LiteralPath $Path
    return "SHA256=$hash SIZE=$($item.Length) LASTWRITE_UTC_TICKS=$($item.LastWriteTimeUtc.Ticks)"
}

function Get-DockerManifestInspectText {
    param(
        [string]$DockerConfig,
        [string]$Reference,
        [switch]$Insecure
    )

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
    return [pscustomobject]@{
        ExitCode = $exitCode
        Text = $text
    }
}

function Wait-RegistryReady {
    param(
        [int]$Port,
        [int]$TimeoutSeconds = 30
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $client = New-Object System.Net.Sockets.TcpClient
        try {
            $async = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
            if ($async.AsyncWaitHandle.WaitOne(500) -and $client.Connected) {
                $client.EndConnect($async)
                return
            }
        }
        catch {
            # Keep waiting.
        }
        finally {
            $client.Close()
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)

    throw "Local registry did not become reachable on 127.0.0.1:$Port within $TimeoutSeconds seconds."
}

function Test-LoopbackPortClosed {
    param([int]$Port)

    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(750)) {
            return $true
        }
        if (-not $client.Connected) {
            return $true
        }
        $client.EndConnect($async)
        return $false
    }
    catch {
        return $true
    }
    finally {
        $client.Close()
    }
}

function Write-Plan {
    param(
        [string]$Branch,
        [string]$Commit,
        [string]$DockerOs,
        [string]$DockerVersion,
        [string]$BuildxVersion,
        [string]$Builder,
        [int64]$FreeGiB
    )

    Write-Host "T-0016 local publisher integration plan"
    Write-Host "  Repository:  $RepositoryRoot"
    Write-Host "  Branch:      $Branch"
    Write-Host "  Commit SHA:  $Commit"
    Write-Host "  Docker OS:   $DockerOs"
    Write-Host "  Docker:      $DockerVersion"
    Write-Host "  Buildx:      $BuildxVersion"
    Write-Host "  Builder:     $Builder"
    Write-Host "  D free GiB:  $FreeGiB"
    Write-Host "  Registry:    disposable official registry bound only to 127.0.0.1"
    Write-Host "  Image:       127.0.0.1:<ephemeral-port>/pharaon-asset-factory"
    Write-Host "  Tags:        sha-<full-commit-sha>, $ReleaseTag"
    Write-Host "  Build:       one Buildx build and direct push; no retries; no prune"
    Write-Host "  Runtime:     pull by digest, health, ready cpu, offline models plan/status"
}

$branch = (git rev-parse --abbrev-ref HEAD).Trim()
$commit = (git rev-parse HEAD).Trim()
if ($commit -notmatch "^[0-9a-f]{40}$") {
    throw "Repository HEAD is not a full 40-character lowercase hex SHA."
}
if ($branch -notlike "ticket/T-0016-*") {
    throw "This script must be run from the T-0016 ticket branch."
}

$dirty = git status --porcelain
if (-not $PreflightOnly -and $dirty) {
    throw "Working tree is not clean. Refusing to run the real integration build."
}
if ($PreflightOnly -and $dirty) {
    Write-Warning "Preflight-only mode is continuing with a dirty working tree; real integration requires a clean tree."
}

$dockerOsLines = & docker info --format "{{.OSType}}" 2>$null
$dockerOs = ($dockerOsLines -join "").Trim()
if ($LASTEXITCODE -ne 0 -or $dockerOs -ne "linux") {
    throw "Docker server is unreachable or OSType is not linux."
}

$dockerVersion = ((& docker version --format "{{.Client.Version}}") -join "").Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Unable to read Docker version."
}

$buildxVersion = ((& docker buildx version) -join " ").Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Docker Buildx is not available."
}

$buildxInspect = & docker buildx inspect
if ($LASTEXITCODE -ne 0) {
    throw "Docker Buildx inspect failed."
}
$builder = "desktop-linux"
$buildxText = (($buildxInspect | ForEach-Object { $_.ToString() }) -join "`n")
$nameMatch = [regex]::Match($buildxText, '(?m)^Name:\s+(.+)$')
if ($nameMatch.Success) {
    $builder = $nameMatch.Groups[1].Value.Trim()
}

$drive = Get-PSDrive -Name D
if (-not $drive) {
    throw "D drive is not available."
}
$requiredFreeBytes = $RequiredFreeGiB * 1GB
if ([int64]$drive.Free -lt $requiredFreeBytes) {
    throw "D drive does not have at least $RequiredFreeGiB GiB free."
}
$freeGiB = [int64][math]::Floor([int64]$drive.Free / 1GB)

Write-Plan -Branch $branch -Commit $commit -DockerOs $dockerOs -DockerVersion $dockerVersion -BuildxVersion $buildxVersion -Builder $builder -FreeGiB $freeGiB

if ($PreflightOnly) {
    Write-Host "PREFLIGHT_ONLY=PASS"
    exit 0
}

if ($Confirmation -eq "") {
    $Confirmation = Read-Host "Type RUN LOCAL PUBLISHER TEST to confirm the single controlled build"
}
if ($Confirmation -ne "RUN LOCAL PUBLISHER TEST") {
    throw "Confirmation phrase was not exactly RUN LOCAL PUBLISHER TEST. No build was started."
}

$normalDockerConfig = Join-Path $env:USERPROFILE ".docker\config.json"
$normalConfigBefore = Get-DockerConfigFingerprint -Path $normalDockerConfig
$runToken = [Guid]::NewGuid().ToString("N")
$tempBase = Join-Path ([System.IO.Path]::GetTempPath()) ("pharaon-t0016-" + $runToken)
New-Item -ItemType Directory -Path $tempBase | Out-Null

$dockerConfig = ""
$registryContainer = ""
$registryPort = 0
$localImage = ""
$metadataFile = Join-Path $tempBase "build-metadata.json"
$buildLog = Join-Path $tempBase "buildx-build.log"
$digest = ""
$digestQualified = ""
$primaryFailure = $null
$primarySucceeded = $false
$evidenceWritten = $false
$cleanupSucceeded = $false
$cleanupFailures = New-Object System.Collections.Generic.List[string]

try {
    $registryPort = Get-AvailableLoopbackPort
    $registryContainer = "$RegistryContainerBase-$commit-$runToken"
    $localImage = "127.0.0.1:$registryPort/pharaon-asset-factory"
    $dockerConfig = New-PublisherTemporaryDockerConfig -BasePath $tempBase -Name "docker-config"

    Write-Host "Starting disposable registry $registryContainer on 127.0.0.1:$registryPort"
    $publishSpec = "127.0.0.1:$($registryPort):5000"
    docker run --detach --name $registryContainer --publish $publishSpec $RegistryImage
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to start disposable registry container."
    }

    Wait-RegistryReady -Port $registryPort
    Write-Host "Disposable registry is reachable."

    $shaTagName = "sha-$commit"
    $tagNames = @($shaTagName, $ReleaseTag)
    $absenceOutputs = @()
    foreach ($tagName in $tagNames) {
        $reference = "${localImage}:${tagName}"
        $rawInspect = Get-DockerManifestInspectText -DockerConfig $dockerConfig -Reference $reference -Insecure
        $absenceOutputs += [pscustomobject]@{
            Tag = $tagName
            ExitCode = $rawInspect.ExitCode
            Text = $rawInspect.Text
        }
        $state = Test-PublisherRegistryTagState -DockerConfig $dockerConfig -Reference $reference -Insecure
        if ($state -ne "Absent") {
            throw "Expected tag to be absent before build, but classifier returned ${state}: ${tagName}"
        }
    }
    Assert-PublisherTagsAbsent -DockerConfig $dockerConfig -Image $localImage -Tags $tagNames -Insecure

    $labels = @{
        "org.opencontainers.image.source" = "https://github.com/kresocts/pharaon-asset-factory"
        "org.opencontainers.image.revision" = $commit
        "org.opencontainers.image.version" = $ReleaseTag
        "org.opencontainers.image.description" = "Pharaon Asset Factory local publisher integration image"
    }

    Write-Host "Starting the single Buildx build and local push. This is the only build attempt."
    $diskBefore = [int64](Get-PSDrive -Name D).Free
    $buildStart = Get-Date
    Start-Transcript -LiteralPath $buildLog -Force
    try {
        Invoke-PublisherBuildxBuild `
            -DockerConfig $dockerConfig `
            -Dockerfile $Dockerfile `
            -Platform $Platform `
            -Context $RepositoryRoot `
            -Image $localImage `
            -Sha $commit `
            -ReleaseTag $ReleaseTag `
            -Labels $labels `
            -MetadataFile $metadataFile
    }
    finally {
        Stop-Transcript
    }
    $buildEnd = Get-Date
    $diskAfter = [int64](Get-PSDrive -Name D).Free
    $buildDuration = $buildEnd - $buildStart

    $digest = Read-PublisherBuildMetadataDigest -MetadataFile $metadataFile
    Assert-PublisherPushedDigest `
        -DockerConfig $dockerConfig `
        -Image $localImage `
        -Sha $commit `
        -ReleaseTag $ReleaseTag `
        -Digest $digest

    $digestQualified = Get-PublisherDigestQualifiedReference -Image $localImage -Digest $digest
    $shaReference = "${localImage}:sha-${commit}"
    $releaseReference = "${localImage}:${ReleaseTag}"
    $shaStateAfter = Test-PublisherRegistryTagState -DockerConfig $dockerConfig -Reference $shaReference -Insecure
    $releaseStateAfter = Test-PublisherRegistryTagState -DockerConfig $dockerConfig -Reference $releaseReference -Insecure
    if ($shaStateAfter -ne "Existing" -or $releaseStateAfter -ne "Existing") {
        throw "Pushed tags were not both classified as existing."
    }

    $refusalMessage = ""
    try {
        Assert-PublisherTagsAbsent -DockerConfig $dockerConfig -Image $localImage -Tags $tagNames -Insecure
        throw "Existing-tag preflight unexpectedly allowed an already-existing tag."
    }
    catch {
        $refusalMessage = $_.Exception.Message
        if ($refusalMessage -notlike "*already exists*") {
            throw
        }
    }

    Write-Host "Pulling by digest: $digestQualified"
    docker --config $dockerConfig pull $digestQualified
    if ($LASTEXITCODE -ne 0) {
        throw "Pull by digest failed."
    }

    Write-Host "Running offline health, readiness, and model-plan checks."
    $healthOutput = & docker --config $dockerConfig run --rm --network none $digestQualified health --json
    if ($LASTEXITCODE -ne 0) {
        throw "Container health check failed."
    }
    $healthText = (($healthOutput | ForEach-Object { $_.ToString() }) -join "`n")

    $readyOutput = & docker --config $dockerConfig run --rm --network none $digestQualified ready --profile cpu --json
    if ($LASTEXITCODE -ne 0) {
        throw "Container CPU readiness check failed."
    }
    $readyText = (($readyOutput | ForEach-Object { $_.ToString() }) -join "`n")
    $ready = $readyText | ConvertFrom-Json
    if ($ready.status -ne "READY" -or -not $ready.ready) {
        throw "CPU readiness was not READY."
    }
    if ($ready.facts.weights.state -ne "ABSENT" -or @($ready.facts.weights.detected_files).Count -ne 0) {
        throw "Unexpected model-weight state in readiness report."
    }

    $planOutput = & docker --config $dockerConfig run --rm --network none $digestQualified models plan --manifest /app/model-manifests/test-fixture.json --json
    if ($LASTEXITCODE -ne 0) {
        throw "Offline model plan failed."
    }
    $planText = (($planOutput | ForEach-Object { $_.ToString() }) -join "`n")
    $plan = $planText | ConvertFrom-Json
    if (-not $plan.success -or $plan.network.requests_attempted -ne 0) {
        throw "Offline model plan did not remain offline or failed."
    }

    $statusOutput = & docker --config $dockerConfig run --rm --network none $digestQualified models status --manifest /app/model-manifests/test-fixture.json --json
    if ($LASTEXITCODE -ne 0) {
        throw "Offline model status failed."
    }
    $statusText = (($statusOutput | ForEach-Object { $_.ToString() }) -join "`n")
    $status = $statusText | ConvertFrom-Json
    if (-not $status.success -or $status.network.requests_attempted -ne 0) {
        throw "Offline model status did not remain offline or failed."
    }


    $primarySucceeded = $true
}
catch {
    $primaryFailure = $_
    Write-Host "PRIMARY_FAILURE=$($_.Exception.Message)"
}
finally {
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $pulledImageRemoved = $true
        if ($digestQualified) {
            docker rmi $digestQualified 2>$null | Out-Null
            & docker image inspect $digestQualified 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) {
                $pulledImageRemoved = $false
            }
        }
        if (-not $pulledImageRemoved) {
            $cleanupFailures.Add("pulled digest image still present")
        }

        $registryGone = $true
        if ($registryContainer) {
            $ids = & docker ps -a --filter "name=^/$registryContainer$" --format "{{.ID}}"
            if ($ids) {
                docker rm --force --volumes $registryContainer 2>$null | Out-Null
            }
            $remaining = & docker ps -a --filter "name=^/$registryContainer$" --format "{{.ID}}"
            if (($remaining | ForEach-Object { $_.ToString() }) -join "") {
                $registryGone = $false
            }
        }
        if (-not $registryGone) {
            $cleanupFailures.Add("registry container still present")
        }

        if (Test-Path -LiteralPath $tempBase) {
            Remove-Item -LiteralPath $tempBase -Recurse -Force -ErrorAction Continue
        }
        $tempDirGone = -not (Test-Path -LiteralPath $tempBase)
        if (-not $tempDirGone) {
            $cleanupFailures.Add("temporary directory still present")
        }

        if ($registryPort -gt 0) {
            $portClosed = Test-LoopbackPortClosed -Port $registryPort
        }
        else {
            $portClosed = $true
        }
        if (-not $portClosed) {
            $cleanupFailures.Add("loopback port still listening")
        }

        $normalConfigAfter = Get-DockerConfigFingerprint -Path $normalDockerConfig
        $normalConfigUnchanged = ($normalConfigAfter -eq $normalConfigBefore)
        if (-not $normalConfigUnchanged) {
            $cleanupFailures.Add("normal Docker config changed")
        }

        docker buildx inspect | Out-Null
        $cacheProbeExit = $LASTEXITCODE
        $cacheAvailable = ($cacheProbeExit -eq 0)
        if (-not $cacheAvailable) {
            $cleanupFailures.Add("Buildx cache unavailable after cleanup")
        }
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
}

$cleanupSucceeded = ($cleanupFailures.Count -eq 0)
$finalExitCode = Get-PublisherProcessExitCode -PrimarySuccess $primarySucceeded -CleanupSuccess $cleanupSucceeded -EvidenceWritten $false

if ($primarySucceeded -and $cleanupSucceeded) {
    $record = @"
# T-0016 local publisher integration validation

Status: PASS
Commit tested: $commit
Branch: $branch

## Environment

- Docker client: $dockerVersion
- Docker server OSType: $dockerOs
- Buildx: $buildxVersion
- Builder: $builder
- D drive free before build: $diskBefore bytes
- D drive free after build: $diskAfter bytes
- Registry image: $RegistryImage
- Loopback binding: 127.0.0.1:$registryPort->5000

## Build

- Exact command: Invoke-PublisherBuildxBuild --file docker/Dockerfile --platform linux/amd64 --provenance=false --sbom=false --push
- Start: $($buildStart.ToString("o"))
- End: $($buildEnd.ToString("o"))
- Duration: $([math]::Round($buildDuration.TotalSeconds, 2)) seconds
- Cache observation: Buildx used the existing default builder without --no-cache, prune, or external cache export. Exact cache reuse was not independently measured from raw logs.

## Tags and digest

- SHA-like tag: $shaTagName
- Release-like tag: $ReleaseTag
- Metadata digest: $digest
- SHA tag digest match: PASS
- Release tag digest match: PASS
- Platform verification: Linux AMD64 PASS
- Digest-qualified reference: $digestQualified

## Existing-tag preflight

- Before build: both tags classified ABSENT.
- After push: both tags classified EXISTING.
- Second preflight refusal: PASS ($refusalMessage)
- Observed absence output:
  - SHA tag exit code: $($absenceOutputs[0].ExitCode)
  - SHA tag first line: $($absenceOutputs[0].Text.Split("`n")[0])
  - Release tag exit code: $($absenceOutputs[1].ExitCode)
  - Release tag first line: $($absenceOutputs[1].Text.Split("`n")[0])

## Runtime checks

- Pull by digest: PASS
- health --json --network none: exit 0
- ready --profile cpu --json --network none: READY
- Weight state: ABSENT, no detected files
- models plan --json --network none: offline PASS
- models status --json --network none: offline PASS

## Cleanup and integrity

- Registry container removed: $registryGone
- Loopback port closed: $portClosed
- Ticket-owned temporary directory removed: $tempDirGone
- Normal Docker config unchanged: $normalConfigUnchanged
- Shared Docker Buildx cache available: $cacheAvailable
- Pulled digest image removed: $pulledImageRemoved
- No GHCR authentication, request, or publication occurred.
- Self-hosted runner remained offline and unused.
- No model weights were downloaded.
- No paid or cloud resources were used.
"@
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $RecordPath) | Out-Null
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText((Resolve-Path -LiteralPath $RecordPath).Path, $record, $utf8NoBom)
    $evidenceWritten = $true
    $finalExitCode = Get-PublisherProcessExitCode -PrimarySuccess $true -CleanupSuccess $true -EvidenceWritten $true
}

if ($primaryFailure) {
    Write-Host "PRIMARY_FAILURE=$($primaryFailure.Exception.Message)"
}
foreach ($failure in $cleanupFailures) {
    Write-Host "CLEANUP_FAILURE=$failure"
}
Write-Host "CLEANUP_REGISTRY_GONE=$registryGone"
Write-Host "CLEANUP_PORT_CLOSED=$portClosed"
Write-Host "CLEANUP_TEMP_DIR_GONE=$tempDirGone"
Write-Host "CLEANUP_NORMAL_DOCKER_CONFIG_UNCHANGED=$normalConfigUnchanged"
Write-Host "CLEANUP_BUILDX_CACHE_AVAILABLE=$cacheAvailable"
Write-Host "CLEANUP_PULLED_IMAGE_REMOVED=$pulledImageRemoved"
Write-Host "RESULT=$(if ($finalExitCode -eq 0) { 'PASS' } else { 'FAIL' })"
exit $finalExitCode
