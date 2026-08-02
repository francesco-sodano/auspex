<#
.SYNOPSIS
Runs staged Form 4 SECURITY_UNRESOLVED recovery through Fabric Notebook 01.

.DESCRIPTION
Submits bounded monthly windows to the canonical nb_01_form4_to_silver item,
passes typed notebook parameters through executionData.parameters, verifies the
effective date window returned by the notebook, and records successful windows
in a local JSONL manifest under .backfill/. Completed windows are skipped on
rerun. Run Notebook 01a, Notebook 03, and Notebook 04 after a recovery batch.
#>
[CmdletBinding()]
param(
    [string]$WorkspaceId = $env:FABRIC_WORKSPACE_ID,
    [string]$NotebookItemId = $env:FABRIC_FORM4_NOTEBOOK_ID,
    [Parameter(Mandatory = $true)]
    [string]$FromDate,
    [Parameter(Mandatory = $true)]
    [string]$ToDate,
    [ValidateRange(1, 12)]
    [int]$WindowMonths = 1,
    [ValidateRange(1, 540)]
    [int]$EdgarRequestsPerMinute = 450,
    [ValidateRange(1, 20)]
    [int]$MaxWorkers = 5,
    [ValidateRange(1, 5000)]
    [int]$WriteBatchSize = 500,
    [ValidateRange(5, 300)]
    [int]$PollSeconds = 15,
    [string]$ManifestPath = "",
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if (-not $WorkspaceId -or -not $NotebookItemId) { throw "WorkspaceId and NotebookItemId are required" }

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$backfillDir = Join-Path $repoRoot ".backfill"
New-Item -ItemType Directory -Path $backfillDir -Force | Out-Null

$from = [datetime]::ParseExact($FromDate, "yyyy-MM-dd", $null)
$to = [datetime]::ParseExact($ToDate, "yyyy-MM-dd", $null)
if ($from -gt $to) {
    throw "FromDate must be on or before ToDate"
}

if ([string]::IsNullOrWhiteSpace($ManifestPath)) {
    $safeFrom = $from.ToString("yyyyMMdd")
    $safeTo = $to.ToString("yyyyMMdd")
    $ManifestPath = Join-Path $backfillDir "form4-entity-recovery-$safeFrom-to-$safeTo.jsonl"
}

function Get-MonthWindows([datetime]$Start, [datetime]$End, [int]$Months) {
    $cursor = $Start
    while ($cursor -le $End) {
        $windowEnd = $cursor.AddMonths($Months).AddDays(-1)
        if ($windowEnd -gt $End) { $windowEnd = $End }

        [pscustomobject]@{
            Start = $cursor.ToString("yyyy-MM-dd")
            End = $windowEnd.ToString("yyyy-MM-dd")
            Key = "$($cursor.ToString('yyyyMMdd'))-$($windowEnd.ToString('yyyyMMdd'))"
        }
        $cursor = $windowEnd.AddDays(1)
    }
}

function Get-CompletedWindows([string]$Path) {
    $completed = @{}
    if (-not (Test-Path $Path)) { return $completed }

    Get-Content $Path | ForEach-Object {
        if (-not [string]::IsNullOrWhiteSpace($_)) {
            $record = $_ | ConvertFrom-Json
            if ($record.status -eq "Completed") {
                $completed[$record.key] = $record
            }
        }
    }
    return $completed
}

function Write-RecoveryRecord([pscustomobject]$Record) {
    ($Record | ConvertTo-Json -Compress -Depth 20) | Add-Content -Path $ManifestPath
}

function Get-FabricToken() {
    $token = az account get-access-token `
        --resource https://api.fabric.microsoft.com `
        --query accessToken `
        --output tsv
    if (-not $token) { throw "Could not acquire a Microsoft Fabric access token" }
    return $token
}

function Invoke-RecoveryWindow($Window) {
    $submittedAt = [datetime]::UtcNow
    if ($DryRun) {
        return [pscustomobject]@{
            key = $Window.Key
            from_date = $Window.Start
            to_date = $Window.End
            status = "DryRun"
            job_id = $null
            submitted_at = $submittedAt.ToString("o")
            completed_at = [datetime]::UtcNow.ToString("o")
            summary = $null
            error = $null
        }
    }

    $headers = @{
        Authorization = "Bearer $(Get-FabricToken)"
        "Content-Type" = "application/json"
    }
    $body = @{
        executionData = @{
            parameters = @{
                from_date = @{ type = "string"; value = $Window.Start }
                to_date = @{ type = "string"; value = $Window.End }
                retry_quarantine_reasons = @{ type = "string"; value = "SECURITY_UNRESOLVED" }
                edgar_requests_per_minute = @{ type = "int"; value = $EdgarRequestsPerMinute }
                max_workers = @{ type = "int"; value = $MaxWorkers }
                write_batch_size = @{ type = "int"; value = $WriteBatchSize }
            }
        }
    } | ConvertTo-Json -Depth 10

    $submitUrl = "https://api.fabric.microsoft.com/v1/workspaces/$WorkspaceId/notebooks/$NotebookItemId/jobs/execute/instances?jobType=RunNotebook"
    $response = Invoke-WebRequest -Uri $submitUrl -Headers $headers -Method Post -Body $body
    $jobId = ([string]$response.Headers.Location -split "/")[-1]
    if (-not $jobId) { throw "Fabric did not return a job ID for $($Window.Key)" }

    $statusUrl = "https://api.fabric.microsoft.com/v1/workspaces/$WorkspaceId/notebooks/$NotebookItemId/jobs/execute/instances/$jobId`?beta=true"
    do {
        Start-Sleep -Seconds $PollSeconds
        try {
            $job = Invoke-RestMethod -Uri $statusUrl -Headers @{ Authorization = "Bearer $(Get-FabricToken)" } -ErrorAction Stop
        } catch {
            $errorText = $_.ErrorDetails.Message
            if (-not $errorText) { $errorText = $_.Exception.Message }
            if ($errorText -match "No notebook execution state found in database") {
                Write-Host "$($Window.Key): execution state not visible yet"
                continue
            }
            throw
        }
        Write-Host "$($Window.Key): $($job.status)"
    } while ($job.status -in @("NotStarted", "InProgress"))

    $summary = $null
    if ($job.properties -and $job.properties.exitValue) {
        $summary = $job.properties.exitValue | ConvertFrom-Json
    }

    $errorText = $null
    if ($job.status -ne "Completed") {
        $errorText = if ($job.failureReason) {
            $job.failureReason | ConvertTo-Json -Compress -Depth 10
        } else {
            "Fabric job ended with status $($job.status)"
        }
    } elseif (-not $summary) {
        $errorText = "Completed Fabric job did not return a notebook summary"
    } elseif ($summary.from_date -ne $Window.Start -or $summary.to_date -ne $Window.End) {
        $errorText = "Effective notebook window $($summary.from_date)..$($summary.to_date) did not match $($Window.Start)..$($Window.End)"
    }

    return [pscustomobject]@{
        key = $Window.Key
        from_date = $Window.Start
        to_date = $Window.End
        status = if ($errorText) { "Failed" } else { "Completed" }
        job_id = $jobId
        submitted_at = $submittedAt.ToString("o")
        completed_at = [datetime]::UtcNow.ToString("o")
        summary = $summary
        error = $errorText
    }
}

$windows = @(Get-MonthWindows $from $to $WindowMonths)
$completed = Get-CompletedWindows $ManifestPath
$pending = @($windows | Where-Object { -not $completed.ContainsKey($_.Key) })

Write-Host "Form 4 entity recovery windows: $($windows.Count) total, $($pending.Count) pending"
Write-Host "Manifest: $ManifestPath"
Write-Host "Notebook: $NotebookItemId in workspace $WorkspaceId"
if ($DryRun) { Write-Host "DRY RUN: no Fabric jobs will be submitted" }

foreach ($window in $pending) {
    Write-Host "Starting $($window.Key): $($window.Start)..$($window.End)"
    $record = Invoke-RecoveryWindow $window
    if (-not $DryRun) { Write-RecoveryRecord $record }

    if ($record.status -eq "DryRun") {
        Write-Host "DRY RUN $($window.Key)"
        continue
    }
    if ($record.status -ne "Completed") {
        throw "Recovery window $($window.Key) failed: $($record.error)"
    }

    Write-Host (
        "$($window.Key) completed: processed=$($record.summary.processed_accessions), " +
        "seeded=$($record.summary.historical_securities_seeded), " +
        "appended=$($record.summary.appended), unresolved=$($record.summary.unresolved)"
    )
}

Write-Host "Entity recovery windows complete. Run Notebook 01a, Notebook 03, and Notebook 04 to convergence."