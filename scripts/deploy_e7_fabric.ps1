[CmdletBinding()]
param(
    [string]$WorkspaceId = $env:FABRIC_WORKSPACE_ID,
    [string]$NotebookPath = "$PSScriptRoot\..\fabric\nb_10_evidence_and_iq.Notebook",
    [string]$OntologyPath = "$PSScriptRoot\..\fabric\auspex_iq_pilot.Ontology",
    [switch]$SkipOntology,
    [switch]$SkipGraphLoad
)

$ErrorActionPreference = "Stop"
if (-not $WorkspaceId) { throw "WorkspaceId or FABRIC_WORKSPACE_ID is required" }
$fabricResource = "https://api.fabric.microsoft.com"
$apiBase = "$fabricResource/v1/workspaces/$WorkspaceId"
$token = az account get-access-token --resource $fabricResource --query accessToken -o tsv
if (-not $token) { throw "Fabric access token is unavailable" }
$headers = @{ Authorization = "Bearer $token"; "Content-Type" = "application/json" }

function Get-DefinitionParts([string]$RootPath) {
    $resolvedRoot = (Resolve-Path $RootPath).Path
    return @(
        Get-ChildItem $resolvedRoot -File -Recurse |
            Where-Object { $_.Extension -ne ".pyc" -and $_.FullName -notmatch "[\\/]__pycache__[\\/]" } |
            Sort-Object FullName | ForEach-Object {
            $relativePath = [IO.Path]::GetRelativePath($resolvedRoot, $_.FullName).Replace("\", "/")
            $text = [IO.File]::ReadAllText($_.FullName) -replace "`r`n", "`n"
            [ordered]@{
                path = $relativePath
                payload = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($text))
                payloadType = "InlineBase64"
            }
        }
    )
}

function Invoke-FabricRequest(
    [string]$Method,
    [string]$Uri,
    [object]$Body = $null
) {
    $parameters = @{
        Method = $Method
        Uri = $Uri
        Headers = $headers
        SkipHttpErrorCheck = $true
    }
    if ($null -ne $Body) {
        $parameters.Body = ($Body | ConvertTo-Json -Depth 30 -Compress)
    }
    $response = Invoke-WebRequest @parameters
    if ([int]$response.StatusCode -ge 400) {
        throw "Fabric request failed ($($response.StatusCode)) $Uri`: $($response.Content)"
    }
    if ([int]$response.StatusCode -ne 202) {
        return $response.Content | ConvertFrom-Json
    }

    $operationUri = @($response.Headers.Location)[0]
    if (-not $operationUri) { $operationUri = @($response.Headers["Operation-Location"])[0] }
    if (-not $operationUri) { throw "Fabric accepted the request without an operation URL" }
    for ($attempt = 0; $attempt -lt 120; $attempt++) {
        Start-Sleep -Seconds 2
        $operation = $null
        for ($transportAttempt = 0; $transportAttempt -lt 5 -and $null -eq $operation; $transportAttempt++) {
            try {
                $operation = Invoke-RestMethod -Method Get -Uri $operationUri -Headers $headers
            } catch {
                if ($transportAttempt -eq 4) { throw }
                Start-Sleep -Seconds 2
            }
        }
        if ($operation.status -eq "Succeeded") {
            $resultUri = $operationUri.TrimEnd("/") + "/result"
            try {
                return Invoke-RestMethod -Method Get -Uri $resultUri -Headers $headers
            } catch {
                return $operation
            }
        }
        if ($operation.status -eq "Failed") {
            throw "Fabric operation failed: $($operation | ConvertTo-Json -Depth 20 -Compress)"
        }
    }
    throw "Fabric operation did not finish within 120 status checks"
}

function Deploy-FabricItem(
    [string]$DisplayName,
    [string]$Type,
    [string]$Path
) {
    $items = Invoke-FabricRequest -Method Get -Uri "$apiBase/items"
    $matches = @($items.value | Where-Object { $_.displayName -eq $DisplayName -and $_.type -eq $Type })
    if ($matches.Count -gt 1) { throw "Multiple $Type items named $DisplayName exist" }

    $definition = @{ parts = Get-DefinitionParts $Path }
    if ($matches.Count -eq 1) {
        $item = $matches[0]
        $result = Invoke-FabricRequest -Method Post -Uri "$apiBase/items/$($item.id)/updateDefinition?updateMetadata=true" -Body @{
            definition = $definition
        }
        return [ordered]@{ displayName = $DisplayName; type = $Type; id = $item.id; action = "updated"; result = $result }
    }

    $result = Invoke-FabricRequest -Method Post -Uri "$apiBase/items" -Body @{
        displayName = $DisplayName
        type = $Type
        definition = $definition
    }
    return [ordered]@{ displayName = $DisplayName; type = $Type; id = $result.id; action = "created"; result = $result }
}

function Save-OntologyGraph([string]$OntologyId) {
    $graphName = "auspex_iq_pilot_graph_$($OntologyId.Replace('-', ''))"
    $graphs = Invoke-FabricRequest -Method Get -Uri "$apiBase/GraphModels"
    $graph = @($graphs.value | Where-Object { $_.displayName -eq $graphName })
    if ($graph.Count -ne 1) {
        throw "Expected one managed Graph model named $graphName, found $($graph.Count)"
    }

    $graphBase = "$apiBase/GraphModels/$($graph[0].id)"
    $definition = Invoke-FabricRequest -Method Post -Uri "$graphBase/getDefinition"
    $parts = @($definition.definition.parts | Where-Object { $_.path -ne ".platform" })
    if ($parts.Count -lt 5) {
        throw "Managed Graph definition is incomplete: parts=$($parts.Count)"
    }
    $jobsBefore = Invoke-FabricRequest -Method Get -Uri "$graphBase/jobs/instances?jobType=Refresh"
    $knownJobIds = @($jobsBefore.value.id)
    Invoke-FabricRequest -Method Post -Uri "$graphBase/updateDefinition" -Body @{
        definition = @{ parts = $parts }
    } | Out-Null

    $latestJob = @()
    for ($attempt = 0; $attempt -lt 30 -and $latestJob.Count -eq 0; $attempt++) {
        Start-Sleep -Seconds 2
        $jobs = Invoke-FabricRequest -Method Get -Uri "$graphBase/jobs/instances?jobType=Refresh"
        $latestJob = @(
            $jobs.value |
                Where-Object { $_.id -notin $knownJobIds } |
                Sort-Object startTimeUtc -Descending |
                Select-Object -First 1
        )
    }
    if ($latestJob.Count -eq 0) {
        throw "Managed Graph save did not create a refresh job"
    }
    return [ordered]@{
        graphId = $graph[0].id
        graphName = $graphName
        queryReadiness = $graph[0].properties.queryReadiness
        refreshJob = if ($latestJob.Count) { $latestJob[0] } else { $null }
    }
}

$results = @(
    Deploy-FabricItem -DisplayName "nb_10_evidence_and_iq" -Type "Notebook" -Path $NotebookPath
)
if (-not $SkipOntology) {
    $ontologyResult = Deploy-FabricItem -DisplayName "auspex_iq_pilot" -Type "Ontology" -Path $OntologyPath
    $results += $ontologyResult
    if (-not $SkipGraphLoad) {
        $results += Save-OntologyGraph -OntologyId $ontologyResult.id
    }
}
$results | ConvertTo-Json -Depth 20

Remove-Variable token