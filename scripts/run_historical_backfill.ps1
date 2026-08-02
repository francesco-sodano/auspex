<#
.SYNOPSIS
Runs a rerunnable Auspex historical connector backfill.

.DESCRIPTION
The script invokes the deployed ingestion Function App in bounded windows and
records completed windows in a local JSONL manifest under .backfill/.
Successful windows are skipped on rerun; failed windows are retried with a new
run_id. This makes the runner operationally rerunnable while the connector and
Fabric Delta layers remain data-idempotent.

Before using -ManageSourceRegistry, temporarily allow your client IP in the
Cosmos DB account networking settings. Restore public access to Disabled after
the backfill.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("dev", "prod")]
    [string]$Environment,
    [Parameter(Mandatory = $true)]
    [string]$BackfillTo,
    [string]$ResourceGroup = "",
    [string]$FunctionApp = "",
    [string]$FabricResourceGroup = "",
    [string]$FabricCapacityName = "",
    [string]$CosmosEndpoint = "",
    [ValidateSet("all", "prices", "sec", "contracts", "news", "alpha-vantage", "etf-holdings")]
    [string[]]$Stage = @("all"),
    [string]$PriceStart = "1999-01-01",
    [string]$SecStart = "2014-01-01",
    [string]$ContractStart = "2008-10-01",
    [ValidateSet("year", "quarter", "month", "day")]
    [string]$ContractWindow = "year",
    [string]$NewsStart = "2025-07-02",
    [int]$PriceSymbolLimit = 10,
    [int]$AlphaVantageSymbolLimit = 2,
    [int]$NewsSymbolLimit = 30,
    [int]$SecFilingLimit = 25,
    [int]$ConnectorMaxAttempts = 3,
    [int]$ConnectorRetryDelaySeconds = 20,
    [string[]]$SecSources = @("sec_form4", "sec_8k", "sec_s1", "sec_13f", "sec_13dg"),
    [string[]]$DailySecSources = @("sec_form4"),
    [object[]]$ContractTerms = @(
        [ordered]@{ symbol = "AAPL"; text = "Apple" },
        [ordered]@{ symbol = "MSFT"; text = "Microsoft" },
        [ordered]@{ symbol = "NVDA"; text = "NVIDIA" }
    ),
    [string[]]$EtfSymbols = @("QQQ", "SMH", "XLK", "XLI", "XLE"),
    [switch]$ManageSourceRegistry,
    [switch]$ManageFabricCapacity,
    [switch]$DisableE8AfterRun,
    [switch]$DryRun
)

if (-not $ResourceGroup) { $ResourceGroup = "auspex-$Environment-ingest" }
if (-not $FunctionApp) { $FunctionApp = "auspex-$Environment-func" }
if (-not $FabricResourceGroup) { $FabricResourceGroup = "auspex-$Environment-data" }
if (-not $FabricCapacityName) { $FabricCapacityName = "auspex$($Environment)fab" }
if (-not $CosmosEndpoint) {
    $CosmosEndpoint = "https://auspex-$Environment-cosmos.documents.azure.com:443/"
}

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$backfillDir = Join-Path $repoRoot ".backfill"
New-Item -ItemType Directory -Path $backfillDir -Force | Out-Null

$manifestSafeTo = $BackfillTo.Replace("-", "")
$stageLabel = ($Stage -join "-").Replace("all", "full")
$BackfillLog = Join-Path $backfillDir "historical-$stageLabel-to-$manifestSafeTo.jsonl"

function Assert-CommandExists($Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command not found: $Name"
    }
}

function Test-Stage($Name) {
    return ($Stage -contains "all") -or ($Stage -contains $Name)
}

function ConvertTo-JsonStable($Value) {
    if ($null -eq $Value) { return "" }
    return ($Value | ConvertTo-Json -Compress -Depth 20)
}

function Get-ObjectValue($Object, [string]$Name, $Default = $null) {
    if ($null -eq $Object) { return $Default }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $Default }
    return $property.Value
}

function Get-ResultRecordsIn($Result) {
    $value = Get-ObjectValue $Result "records_in" 0
    if ($null -eq $value -or $value -eq "") { return 0 }
    return [int]$value
}

function Test-TerminalPagedResult($Result) {
    $status = Get-ObjectValue $Result "status" ""
    $hasMore = Get-ObjectValue $Result "has_more" $null
    return $status -in @("ok", "empty", "already-completed", "dryrun") -and $hasMore -eq $false
}

function Test-TransientBackfillError($Result) {
    if ($null -eq $Result) { return $false }
    if ((Get-ObjectValue $Result "status" "") -ne "failed") { return $false }

    $errorText = [string](Get-ObjectValue $Result "error" "")
    return $errorText -match "Fabric capacity|Gateway Timeout|Gateway Time-out|504|500 Internal Server Error|502|503|Service Unavailable|temporarily unavailable|not yet available|Too Many Requests|429|timed out|timeout|No such host|sending the request|connection|DNS|ConditionNotMet|condition specified"
}

function Test-FailedBackfillResult($Result) {
    $status = Get-ObjectValue $Result "status" "failed"
    $errorText = [string](Get-ObjectValue $Result "error" "")
    return ($status -eq "failed") -or ($status -eq "skipped" -and -not [string]::IsNullOrWhiteSpace($errorText))
}

function Get-CompletedBackfillKeys($Path) {
    $completed = @{}
    if (Test-Path $Path) {
        Get-Content $Path | ForEach-Object {
            if ($_ -and $_.Trim()) {
                $row = $_ | ConvertFrom-Json
                $skippedByDedup = ($row.status -eq "skipped" -and [string]::IsNullOrWhiteSpace([string]$row.error))
                if ($row.status -in @("ok", "empty") -or $skippedByDedup) {
                    $completed[$row.key] = $row
                }
            }
        }
    }
    return $completed
}

function Write-BackfillLog($Path, $Record) {
    ($Record | ConvertTo-Json -Compress -Depth 20) | Add-Content -Path $Path
}

function Invoke-FabricCapacityAction([string]$Action) {
    if (-not $ManageFabricCapacity) { return }
    if ($DryRun) {
        Write-Host "DRY RUN Fabric capacity $Action $FabricCapacityName"
        return
    }

    $resourceId = az resource show `
        --resource-group $FabricResourceGroup `
        --name $FabricCapacityName `
        --resource-type Microsoft.Fabric/capacities `
        --api-version 2023-11-01 `
        --query id `
        -o tsv
    if (-not $resourceId) {
        throw "Could not resolve Fabric capacity $FabricCapacityName in $FabricResourceGroup"
    }

    $state = az resource show `
        --ids $resourceId `
        --api-version 2023-11-01 `
        --query properties.state `
        -o tsv
    if ($Action -eq "resume" -and $state -eq "Active") {
        Write-Host "Fabric capacity $FabricCapacityName already active"
        return
    }
    if ($Action -eq "suspend" -and $state -in @("Paused", "Suspended")) {
        Write-Host "Fabric capacity $FabricCapacityName already paused"
        return
    }

    Write-Host "Fabric capacity $Action $FabricCapacityName"
    az rest `
        --method post `
        --url "https://management.azure.com$resourceId/$Action`?api-version=2023-11-01" `
        --only-show-errors | Out-Null
}

function Resume-FabricCapacityForBackfill() {
    Invoke-FabricCapacityAction "resume"
}

function Suspend-FabricCapacityForBackfill() {
    Invoke-FabricCapacityAction "suspend"
}

function Get-MonthWindows($StartDate, $EndDate) {
    $cursor = [datetime]$StartDate
    $limit = [datetime]$EndDate
    while ($cursor -le $limit) {
        $monthEnd = $cursor.AddMonths(1).AddDays(-1)
        if ($monthEnd -gt $limit) { $monthEnd = $limit }

        [pscustomobject]@{
            Start = $cursor.ToString("yyyy-MM-dd")
            End = $monthEnd.ToString("yyyy-MM-dd")
            Key = $cursor.ToString("yyyyMM")
        }

        $cursor = $monthEnd.AddDays(1)
    }
}

function Get-QuarterWindows($StartDate, $EndDate) {
    $cursor = [datetime]$StartDate
    $limit = [datetime]$EndDate
    while ($cursor -le $limit) {
        $quarterEnd = $cursor.AddMonths(3).AddDays(-1)
        if ($quarterEnd -gt $limit) { $quarterEnd = $limit }

        [pscustomobject]@{
            Start = $cursor.ToString("yyyy-MM-dd")
            End = $quarterEnd.ToString("yyyy-MM-dd")
            Key = $cursor.ToString("yyyyMM")
        }

        $cursor = $quarterEnd.AddDays(1)
    }
}

function Get-DayWindows($StartDate, $EndDate) {
    $cursor = [datetime]$StartDate
    $limit = [datetime]$EndDate
    while ($cursor -le $limit) {
        [pscustomobject]@{
            Start = $cursor.ToString("yyyy-MM-dd")
            End = $cursor.ToString("yyyy-MM-dd")
            Key = $cursor.ToString("yyyyMMdd")
        }

        $cursor = $cursor.AddDays(1)
    }
}

function Get-YearWindows($StartDate, $EndDate) {
    $cursor = [datetime]$StartDate
    $limit = [datetime]$EndDate
    while ($cursor -le $limit) {
        $yearEnd = [datetime]("$($cursor.Year)-12-31")
        if ($yearEnd -gt $limit) { $yearEnd = $limit }

        [pscustomobject]@{
            Start = $cursor.ToString("yyyy-MM-dd")
            End = $yearEnd.ToString("yyyy-MM-dd")
            Key = $cursor.ToString("yyyy")
        }

        $cursor = $yearEnd.AddDays(1)
    }
}

function Invoke-AuspexConnectorSafe($Payload) {
    $body = $Payload | ConvertTo-Json -Depth 20
    if ($DryRun) {
        Write-Host "DRY RUN $($Payload.source_id) $($Payload.run_id)"
        return [pscustomobject]@{
            run_id = $Payload.run_id
            source_id = $Payload.source_id
            status = "dryrun"
            records_in = 0
            bytes_written = 0
            error = $null
            has_more = $false
        }
    }

    $originalRunId = $Payload.run_id
    $attempts = [Math]::Max(1, $ConnectorMaxAttempts)
    for ($attempt = 1; $attempt -le $attempts; $attempt++) {
        if ($attempt -gt 1) {
            $Payload.run_id = "$originalRunId-retry$attempt"
            $body = $Payload | ConvertTo-Json -Depth 20
        }

        $result = $null
        try {
            $result = Invoke-RestMethod `
                -Method Post `
                -Uri $script:FunctionUri `
                -Headers @{ "x-functions-key" = $script:FunctionKey } `
                -ContentType "application/json" `
                -Body $body
        }
        catch {
            if ($_.ErrorDetails.Message) {
                try {
                    $result = ($_.ErrorDetails.Message | ConvertFrom-Json)
                }
                catch {}
            }

            if ($null -eq $result) {
                $result = [pscustomobject]@{
                    run_id = $Payload.run_id
                    source_id = $Payload.source_id
                    status = "failed"
                    records_in = 0
                    bytes_written = 0
                    error = $_.Exception.Message
                }
            }
        }

        if (-not (Test-TransientBackfillError $result) -or $attempt -eq $attempts) {
            return $result
        }

        $errorText = Get-ObjectValue $result "error" ""
        Write-Warning "Transient failure for $originalRunId on attempt $attempt/$attempts. Retrying in $ConnectorRetryDelaySeconds seconds. Error: $errorText"
        Start-Sleep -Seconds $ConnectorRetryDelaySeconds
    }
}

function Invoke-BackfillPayload($SourceId, $SinceDate, $ToDate, $WindowKey, $Extra = @{}, [bool]$RequirePaginationState = $false) {
    $keyParts = @($SourceId, $SinceDate, $ToDate)
    foreach ($extraKey in ($Extra.Keys | Sort-Object)) {
        $keyParts += "$extraKey=$(ConvertTo-JsonStable $Extra[$extraKey])"
    }
    $key = $keyParts -join "|"
    $completed = Get-CompletedBackfillKeys $BackfillLog

    $completedRow = if ($completed.ContainsKey($key)) { $completed[$key] } else { $null }
    $hasPaginationState = $null -ne $completedRow -and $null -ne $completedRow.PSObject.Properties["has_more"]
    if ($null -ne $completedRow -and (-not $RequirePaginationState -or $hasPaginationState)) {
        Write-Host "SKIP completed $key"
        return [pscustomobject]@{
            key = $key
            source_id = $SourceId
            since_date = $SinceDate
            to_date = $ToDate
            run_id = Get-ObjectValue $completedRow "run_id" $null
            status = "already-completed"
            records_in = Get-ObjectValue $completedRow "records_in" 0
            bytes_written = Get-ObjectValue $completedRow "bytes_written" 0
            error = $null
            has_more = Get-ObjectValue $completedRow "has_more" $null
            logged_at = Get-ObjectValue $completedRow "logged_at" $null
        }
    }

    $runId = "hist-$SourceId-$WindowKey-$manifestSafeTo-$((New-Guid).Guid.Substring(0, 8))"
    $payload = @{
        source_id = $SourceId
        mode = "backfill"
        since_date = $SinceDate
        to_date = $ToDate
        run_id = $runId
    }
    foreach ($extraKey in $Extra.Keys) {
        $payload[$extraKey] = $Extra[$extraKey]
    }

    $result = Invoke-AuspexConnectorSafe $payload
    $record = [pscustomobject]@{
        key = $key
        source_id = $SourceId
        since_date = $SinceDate
        to_date = $ToDate
        run_id = Get-ObjectValue $result "run_id" $runId
        status = Get-ObjectValue $result "status" "failed"
        records_in = Get-ObjectValue $result "records_in" 0
        bytes_written = Get-ObjectValue $result "bytes_written" 0
        error = Get-ObjectValue $result "error" $null
        has_more = Get-ObjectValue $result "has_more" $null
        logged_at = (Get-Date).ToString("o")
    }
    if (-not $DryRun) {
        Write-BackfillLog $BackfillLog $record
    }
    return $record
}

function Set-E8SourcesEnabled($Enabled) {
    if (-not $ManageSourceRegistry) { return }

    $enabledText = if ($Enabled) { "true" } else { "false" }
    $contractTermsJson = $ContractTerms | ConvertTo-Json -Compress -Depth 10
    $python = @"
import json
import os
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential

endpoint = os.environ["COSMOS_ENDPOINT"]
terms = json.loads(os.environ.get("AUSPEX_CONTRACT_TERMS", "[]"))
enabled = os.environ["AUSPEX_E8_ENABLED"].lower() == "true"
container = (
    CosmosClient(endpoint, DefaultAzureCredential())
    .get_database_client("auspex")
    .get_container_client("sources")
)

for source_id in ["alpha_vantage", "news", "contracts", "sec_13f", "sec_13dg", "sec_8k", "sec_s1", "etf_holdings"]:
    doc = container.read_item(item=source_id, partition_key=source_id)
    doc["enabled"] = enabled
    if source_id == "contracts" and terms:
        doc["search_terms"] = terms
    container.upsert_item(doc)
    print(f"{source_id} enabled: {doc['enabled']}")
"@

    if ($DryRun) {
        Write-Host "DRY RUN set E8 sources enabled=$Enabled"
        return
    }

    $env:COSMOS_ENDPOINT = $CosmosEndpoint
    $env:PYTHONPATH = $repoRoot
    $env:AUSPEX_CONTRACT_TERMS = $contractTermsJson
    $env:AUSPEX_E8_ENABLED = $enabledText
    $python | python -
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to set E8 source enabled=$Enabled in Cosmos source registry. Check Cosmos firewall access for this client IP."
    }
}

Assert-CommandExists az

$script:FunctionKey = az functionapp keys list `
    --resource-group $ResourceGroup `
    --name $FunctionApp `
    --query "functionKeys.default" `
    -o tsv

if (-not $script:FunctionKey) {
    throw "Could not resolve host function key for $FunctionApp"
}

$script:FunctionUri = "https://$FunctionApp.azurewebsites.net/api/run"

Write-Host "Backfill log: $BackfillLog"
Write-Host "Function URI: $script:FunctionUri"
Write-Host "Backfill end date: $BackfillTo"

if ($ManageSourceRegistry) {
    Set-E8SourcesEnabled $true
}

Resume-FabricCapacityForBackfill

$failures = @()

try {
    if (Test-Stage "prices") {
        $offset = 0
        while ($true) {
            $result = Invoke-BackfillPayload "prices_eod" $PriceStart $BackfillTo "offset-$offset" @{
                outputsize = "full"
                symbol_offset = $offset
                symbol_limit = $PriceSymbolLimit
            } $true
            $result
            if (Test-FailedBackfillResult $result) { $failures += $result }
            if (Test-TerminalPagedResult $result) { break }
            $offset += $PriceSymbolLimit
        }
    }

    if (Test-Stage "sec") {
        foreach ($source in $SecSources) {
            $windows = if ($DailySecSources -contains $source) {
                Get-DayWindows $SecStart $BackfillTo
            }
            else {
                Get-MonthWindows $SecStart $BackfillTo
            }

            foreach ($window in $windows) {
                $offset = 0
                while ($true) {
                    $result = Invoke-BackfillPayload $source $window.Start $window.End "$($window.Key)-offset-$offset" @{
                        filing_offset = $offset
                        filing_limit = $SecFilingLimit
                    } $true
                    $result
                    if (Test-FailedBackfillResult $result) { $failures += $result; break }
                    if (Test-TerminalPagedResult $result) { break }
                    $offset += $SecFilingLimit
                }
            }
        }
    }

    if (Test-Stage "contracts") {
        $contractWindows = switch ($ContractWindow) {
            "day" { Get-DayWindows $ContractStart $BackfillTo }
            "month" { Get-MonthWindows $ContractStart $BackfillTo }
            "quarter" { Get-QuarterWindows $ContractStart $BackfillTo }
            default { Get-YearWindows $ContractStart $BackfillTo }
        }
        foreach ($window in $contractWindows) {
            $result = Invoke-BackfillPayload "contracts" $window.Start $window.End $window.Key @{
                search_terms = $ContractTerms
            }
            $result
            if (Test-FailedBackfillResult $result) { $failures += $result }
        }
    }

    if (Test-Stage "news") {
        $offset = 0
        while ($true) {
            $result = Invoke-BackfillPayload "news" $NewsStart $BackfillTo "offset-$offset" @{
                symbol_offset = $offset
                symbol_limit = $NewsSymbolLimit
            } $true
            $result
            if (Test-FailedBackfillResult $result) { $failures += $result }
            if (Test-TerminalPagedResult $result) { break }
            $offset += $NewsSymbolLimit
        }
    }

    if (Test-Stage "alpha-vantage") {
        $snapshotDate = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd")
        $offset = 0
        while ($true) {
            $result = Invoke-BackfillPayload "alpha_vantage" $snapshotDate $snapshotDate "snapshot-$snapshotDate-offset-$offset" @{
                symbol_offset = $offset
                symbol_limit = $AlphaVantageSymbolLimit
                include_etfs = $false
                include_global = ($offset -eq 0)
            } $true
            $result
            if (Test-FailedBackfillResult $result) { $failures += $result }
            if (Test-TerminalPagedResult $result) { break }
            $offset += $AlphaVantageSymbolLimit
        }
    }

    if (Test-Stage "etf-holdings") {
        $result = Invoke-BackfillPayload "etf_holdings" $BackfillTo $BackfillTo "current" @{
            etf_symbols = $EtfSymbols
        }
        $result
        if (Test-FailedBackfillResult $result) { $failures += $result }
    }
}
finally {
    if ($DisableE8AfterRun) {
        Set-E8SourcesEnabled $false
    }
    if ($ManageFabricCapacity) {
        Suspend-FabricCapacityForBackfill
    }
}

if ($failures.Count -gt 0) {
    Write-Warning "Backfill completed with $($failures.Count) failed windows. Check $BackfillLog and rerun after fixes."
    $failures | Format-Table -AutoSize
    exit 1
}

Write-Host "Backfill completed without failed windows. Manifest: $BackfillLog"