#Requires -Version 7.2
[CmdletBinding()]
param(
    [ValidateSet('Deploy', 'Run', 'Destroy')]
    [string]$Action = 'Deploy',

    [string]$SubscriptionId,

    [ValidatePattern('^[a-z0-9]+$')]
    [string]$Location = 'westeurope',

    [ValidatePattern('^[a-z][a-z0-9-]{1,30}[a-z0-9]$')]
    [string]$LabName = 'getpractical-sandboxes-lab',

    [string]$PrincipalId,

    [ValidateSet('User', 'Group', 'ServicePrincipal')]
    [string]$PrincipalType = 'User',

    [string]$StateFile = (Join-Path $PSScriptRoot '.local\environment.json'),

    [ValidateRange(1, 20)]
    [int]$Samples = 5,

    [switch]$RunLab,

    [string]$ConfirmResourceGroup
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$StateFile = [IO.Path]::GetFullPath($StateFile)

function Invoke-AzJson {
    param([Parameter(Mandatory)][string[]]$Arguments)
    $output = & az @Arguments --only-show-errors --output json
    if ($LASTEXITCODE -ne 0) {
        throw "Azure CLI failed with exit code $LASTEXITCODE. Command: az $($Arguments -join ' ')"
    }
    $text = $output -join "`n"
    if (-not [string]::IsNullOrWhiteSpace($text)) {
        return ConvertFrom-Json -InputObject $text -AsHashtable
    }
}

function Save-State {
    param([Parameter(Mandatory)][hashtable]$Value)
    $parent = Split-Path -Parent $StateFile
    [IO.Directory]::CreateDirectory($parent) | Out-Null
    $temporary = "$StateFile.tmp"
    [IO.File]::WriteAllText($temporary, ($Value | ConvertTo-Json -Depth 8))
    Move-Item -LiteralPath $temporary -Destination $StateFile -Force
}

function Assert-LabOwnership {
    param([Parameter(Mandatory)][hashtable]$State)
    $group = Invoke-AzJson @(
        'group', 'show', '--name', $State.resource_group,
        '--subscription', $State.subscription_id
    )
    if ($group.tags.managedBy -ne 'aca-sandboxes-lab' -or $group.tags.labId -ne $State.lab_id) {
        throw 'Resource-group ownership tags do not match the local state. No changes were made.'
    }
}

function Initialize-Python {
    $python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        Get-Command python -ErrorAction Stop | Out-Null
        & python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'
        if ($LASTEXITCODE -ne 0) {
            throw 'Python 3.12 or newer is required on the Windows workstation.'
        }
        & python -m venv (Join-Path $PSScriptRoot '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the local Python virtual environment.' }
    }
    $requirements = Join-Path $PSScriptRoot 'requirements.txt'
    $dependencyCheck = @'
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
missing = []
for line in Path(sys.argv[1]).read_text().splitlines():
    if not line.strip() or line.startswith("#"):
        continue
    name, required = line.strip().split("==", 1)
    try:
        if version(name) != required:
            missing.append(line)
    except PackageNotFoundError:
        missing.append(line)
if missing:
    print("Dependencies to install: " + ", ".join(missing))
    sys.exit(1)
'@
    & $python -c $dependencyCheck $requirements | Out-Host
    if ($LASTEXITCODE -eq 1) {
        & $python -m pip install --quiet -r $requirements | Out-Host
        if ($LASTEXITCODE -ne 0) { throw 'Installing the pinned Python dependencies failed.' }
    }
    elseif ($LASTEXITCODE -ne 0) {
        throw 'Checking the Python dependencies failed.'
    }
    & $python -m pip check | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'The Python environment has conflicting dependencies.' }
    return $python
}

Get-Command az -ErrorAction Stop | Out-Null
$state = $null
if (Test-Path -LiteralPath $StateFile) {
    $state = Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json -AsHashtable
    if ($state.schema_version -ne 1) { throw 'Unsupported environment state schema.' }
    if ($SubscriptionId -and $SubscriptionId -ne $state.subscription_id) {
        throw 'The subscription differs from the existing state. Use a different -StateFile for a new lab.'
    }
    if ($PSBoundParameters.ContainsKey('Location') -and $Location -ne $state.region) {
        throw 'The region differs from the existing state. Use a different -StateFile for a new lab.'
    }
    if ($PSBoundParameters.ContainsKey('LabName') -and $LabName -ne $state.sandbox_group) {
        throw 'The lab name differs from the existing state. Use a different -StateFile for a new lab.'
    }
}

if ($Action -eq 'Deploy') {
    if (-not $state) {
        if (-not $SubscriptionId) { throw 'Specify -SubscriptionId for the first deployment.' }
        [guid]::Parse($SubscriptionId) | Out-Null
        $account = Invoke-AzJson @('account', 'show', '--subscription', $SubscriptionId)
        if ($account.environmentName -ne 'AzureCloud' -or $account.state -ne 'Enabled') {
            throw 'This lab requires an enabled subscription in the Azure public cloud.'
        }
        if (-not $PrincipalId) {
            if ($PrincipalType -ne 'User') {
                throw 'Specify the Entra object ID with -PrincipalId for a group or service principal.'
            }
            $activeTenant = Invoke-AzJson @('account', 'show', '--query', 'tenantId')
            if ($activeTenant -ne $account.tenantId) {
                throw 'The active CLI account is in a different tenant. Sign in to the target tenant or supply -PrincipalId explicitly.'
            }
            $PrincipalId = Invoke-AzJson @(
                'ad', 'signed-in-user', 'show', '--query', 'id'
            )
        }
        [guid]::Parse($PrincipalId) | Out-Null
        $labId = [guid]::NewGuid().ToString('N')
        $nameExists = Invoke-AzJson @(
            'group', 'exists', '--name', "rg-$LabName", '--subscription', $SubscriptionId
        )
        if ($nameExists) {
            throw "Resource group 'rg-$LabName' already exists. Reuse its original state file, or choose a different -LabName. It will not be adopted."
        }
        $state = @{
            schema_version = 1
            lab_id = $labId
            subscription_id = $SubscriptionId
            tenant_id = $account.tenantId
            region = $Location
            resource_group = "rg-$LabName"
            sandbox_group = $LabName
            principal_id = $PrincipalId
            principal_type = $PrincipalType
            image = 'python-3.12'
            created_utc = [DateTime]::UtcNow.ToString('o')
            status = 'planned'
        }
        Save-State $state
    }
    if ($state.status -eq 'destroyed') {
        throw 'This lab was destroyed. Use a new -StateFile to build a new lab.'
    }
    if ($PrincipalId -and $PrincipalId -ne $state.principal_id) {
        throw 'The principal differs from the existing state. Role changes require explicit administration.'
    }

    Write-Host "Subscription: $($state.subscription_id)"
    Write-Host "Region:       $($state.region)"
    Write-Host "Lab group:    $($state.resource_group)"
    Write-Host 'The deployment creates Azure resources. Running the lab incurs compute and possible storage charges.'

    $provider = Invoke-AzJson @(
        'provider', 'show', '--namespace', 'Microsoft.App', '--subscription', $state.subscription_id
    )
    if ($provider.registrationState -ne 'Registered') {
        Write-Host 'Registering Microsoft.App for the selected subscription.'
        Invoke-AzJson @(
            'provider', 'register', '--namespace', 'Microsoft.App', '--wait',
            '--subscription', $state.subscription_id
        ) | Out-Null
        $provider = Invoke-AzJson @(
            'provider', 'show', '--namespace', 'Microsoft.App', '--subscription', $state.subscription_id
        )
    }
    $resourceType = @($provider.resourceTypes | Where-Object resourceType -EQ 'sandboxGroups')
    if ($resourceType.Count -ne 1 -or $resourceType[0].apiVersions -notcontains '2026-07-01') {
        throw 'The subscription does not advertise the 2026-07-01 sandboxGroups API.'
    }
    $locations = @($resourceType[0].locations | ForEach-Object { ($_ -replace ' ', '').ToLowerInvariant() })
    if ($locations -notcontains $state.region) {
        throw "Sandbox groups are not advertised in $($state.region). Choose a supported region explicitly."
    }

    $python = Initialize-Python
    $template = Join-Path $PSScriptRoot 'infra\main.bicep'
    $compiled = Join-Path (Split-Path -Parent $StateFile) 'main.json'
    & az bicep build --file $template --outfile $compiled --only-show-errors
    if ($LASTEXITCODE -ne 0) {
        throw 'Bicep compilation failed. Install or update Bicep with az bicep install or az bicep upgrade.'
    }

    $exists = Invoke-AzJson @(
        'group', 'exists', '--name', $state.resource_group, '--subscription', $state.subscription_id
    )
    if ($exists) {
        Assert-LabOwnership $state
    }
    else {
        Invoke-AzJson @(
            'group', 'create', '--name', $state.resource_group, '--location', $state.region,
            '--tags', 'managedBy=aca-sandboxes-lab', "labId=$($state.lab_id)",
            '--subscription', $state.subscription_id
        ) | Out-Null
    }
    $state.status = 'provisioning'
    Save-State $state
    Invoke-AzJson @(
        'deployment', 'group', 'create', '--name', 'aca-sandboxes-lab',
        '--resource-group', $state.resource_group, '--subscription', $state.subscription_id,
        '--template-file', $compiled, '--parameters',
        "location=$($state.region)", "sandboxGroupName=$($state.sandbox_group)",
        "principalId=$($state.principal_id)", "principalType=$($state.principal_type)",
        "labId=$($state.lab_id)"
    ) | Out-Null

    & $python (Join-Path $PSScriptRoot 'lab.py') preflight --config $StateFile
    if ($LASTEXITCODE -ne 0) {
        throw 'The ARM deployment exists, but the data-plane preflight failed. See the error above; state was retained.'
    }
    $state.status = 'ready'
    Save-State $state
    Write-Host "Environment ready. Configuration: $StateFile"
}

if ($Action -eq 'Run' -or ($Action -eq 'Deploy' -and $RunLab)) {
    if (-not $state -or $state.status -ne 'ready') {
        throw 'Deploy the environment successfully before running the experiments.'
    }
    Assert-LabOwnership $state
    if ($Action -eq 'Run') { $python = Initialize-Python }
    & $python (Join-Path $PSScriptRoot 'lab.py') run --config $StateFile --samples $Samples
    if ($LASTEXITCODE -ne 0) {
        throw 'The lab failed. Inspect its report and cleanup results before running it again.'
    }
}

if ($Action -eq 'Destroy') {
    if (-not $state) { throw 'There is no environment state file to identify the lab.' }
    if ($ConfirmResourceGroup -ne $state.resource_group) {
        throw "Destruction requires -ConfirmResourceGroup '$($state.resource_group)'. This deletes all data in that lab group."
    }
    $exists = Invoke-AzJson @(
        'group', 'exists', '--name', $state.resource_group, '--subscription', $state.subscription_id
    )
    if ($exists) {
        Assert-LabOwnership $state
        $expected = "/subscriptions/$($state.subscription_id)/resourceGroups/$($state.resource_group)/providers/Microsoft.App/sandboxGroups/$($state.sandbox_group)"
        $resources = @(Invoke-AzJson @(
            'resource', 'list', '--resource-group', $state.resource_group,
            '--subscription', $state.subscription_id
        ))
        $unexpected = @($resources | Where-Object { $_.id -ine $expected })
        if ($unexpected.Count -gt 0) {
            throw 'The resource group contains resources outside this lab. Review it manually; automatic deletion was refused.'
        }
        Invoke-AzJson @(
            'group', 'delete', '--name', $state.resource_group, '--yes',
            '--subscription', $state.subscription_id
        ) | Out-Null
        $stillExists = Invoke-AzJson @(
            'group', 'exists', '--name', $state.resource_group, '--subscription', $state.subscription_id
        )
        if ($stillExists) { throw 'Azure still reports the resource group after deletion.' }
    }
    $state.status = 'destroyed'
    Save-State $state
    Write-Host 'The lab resource group was removed. Local reports and configuration were retained.'
}
