# GetPractical Azure Container Apps Sandboxes lab

[Repository](https://github.com/jpsantoscosta/getpractical-sandboxes-lab) |
[Latest release](https://github.com/jpsantoscosta/getpractical-sandboxes-lab/releases/latest) |
[Download ZIP](https://github.com/jpsantoscosta/getpractical-sandboxes-lab/releases/latest/download/getpractical-sandboxes-lab.zip) |
[MIT license](LICENSE)

An independent Windows lab for Azure Container Apps Sandboxes: deploy a dedicated group, compare memory and disk state, clone a checkpoint, exercise an egress policy, attach persistent storage, and measure startup. It is not an official Microsoft product.

The runner performs 21 behavioral checks against live Azure and cleans up the sandboxes, snapshots and volume it creates. The resource group and sandbox group remain until you invoke guarded `Destroy`.

**Evidence:** the [bundled results](evidence/results.json) are historical curated measurements from West Europe on **25 September 2026**, not example output and **not a fresh run of the revised v1.0.0 release**. Original check verdicts, timings and the source-report SHA256 are retained. Local checks cannot establish current cloud behavior.

## Read this before running

**Running this lab creates billable Azure resources.** Use a dedicated resource group, review the [current cost guidance](https://sandboxes.azure.com/docs/sandboxes/cost), and run cleanup when finished. There is no cost cap or automatic budget alert.

The snapshot experiment restores only the supplied, trusted loopback HTTP server. Do not replace it with untrusted code, credentials, customer data, payment logic, queue consumers or other software with external side effects. **Applying policy after restore is not a first-instruction isolation guarantee for arbitrary untrusted workloads.** Fresh sandboxes receive the deny policy in their create request.

This is an engineering lab, not a production agent-execution service. It does not test hypervisor escapes, adversarial resource exhaustion, disaster recovery, network protocol bypasses or exactly-once execution.

Service GA, a stable ARM control plane and a beta data-plane SDK are separate things. This release deliberately pairs `Microsoft.App/sandboxGroups@2026-07-01` with `azure-containerapps-sandbox==0.1.0b4` and that SDK's default `2026-02-01-preview` data-plane API. **Do not set the SDK's `api_version` to the ARM version or the newest REST version.** See the maintainer's compatibility guidance in [microsoft/azure-container-apps#1839](https://github.com/microsoft/azure-container-apps/issues/1839).

## What gets built

| Resource or setting | Configuration |
| --- | --- |
| Resource group | `rg-getpractical-sandboxes-lab` |
| Sandbox group | `getpractical-sandboxes-lab` |
| ARM API | `Microsoft.App/sandboxGroups@2026-07-01` |
| Data-plane client | `azure-containerapps-sandbox==0.1.0b4`, using its `2026-02-01-preview` API |
| Operator permission | `Container Apps SandboxGroup Data Owner`, scoped to this sandbox group |
| Guest image | Public `python-3.12`, checked against the regional catalog |
| Core and timing experiments | 1 vCPU, 2 GiB memory, 20 GiB root disk |
| Data disk experiments | 2 vCPU, 4 GiB memory, 20 GiB root disk, 1 GiB data disk |
| Fresh sandbox egress | Default `Deny`, `Full` inspection, allow only `GET https://api.github.com/zen` |
| Guest ingress | No exposed ports. The state server binds only to `127.0.0.1:8765` |
| Idle policy | Suspend after 300 seconds of platform-defined inactivity |
| Retention policy | Delete after 3,600 seconds stopped |
| Secrets and credentials | Azure CLI credentials stay on the workstation; none are passed to the guest |

The full run creates 11 sandboxes when `-Samples 5` is used. They do not all run together. Normal execution uses at most two concurrently. The highest concurrent allocation is the data disk experiment with two 2-vCPU sandboxes.

Fresh sandboxes carry readable `name` labels: `state-and-network-demo`, `filesystem-isolation-check`, `storage-writer`, `second-mount-check`, `storage-reader`, and `startup-check-01` through `startup-check-05`. The controller calls the snapshot clone `restored-memory-demo`. The pinned Python SDK rejects label overrides during restore; this is **not a service-wide limitation**, and [microsoft/azure-container-apps#1810](https://github.com/microsoft/azure-container-apps/issues/1810) documents CLI support. Azure still generates internal sandbox IDs, which the controller retains for API calls and cleanup.

Snapshots and volumes use `memory-checkpoint` and `working-data` prefixes followed by a UTC timestamp, so later runs are distinguishable without random-looking names.

No managed Container Apps environment, ACR, model deployment, VNet, private endpoint, storage account or Log Analytics workspace is required for this lab. The Data Disk is a group-scoped sandbox volume, not an ARM `Microsoft.Compute/disks` resource created by the template.

## Prerequisites

Use Windows with PowerShell 7.2 or later, Azure CLI, and Python 3.12 or later. The live run used PowerShell 7.6.6, Azure CLI 2.88.0, Python 3.14.3 and Bicep 0.45.15.

The signed-in Entra identity needs permission to create a resource group and deploy its resources, plus permission to create role assignments. `Owner` provides both. `Contributor` alone cannot assign the sandbox data-plane role. `User Access Administrator` alone does not provide resource-deployment permissions.

Use an Azure public-cloud subscription in a supported region. Personal Microsoft accounts are not supported by the sandbox service. This lab does not install Azure CLI or change the machine's execution policy.

Run these commands first:

```powershell
az login
az bicep version
python --version
$PSVersionTable.PSVersion
```

If Bicep is absent:

```powershell
az bicep install
```

Use `az login --tenant '<tenant-id>'` when you need a specific tenant. If the active CLI account belongs to another tenant, the script refuses to infer your object ID. Sign in to the correct tenant or supply the target tenant's Entra object ID with `-PrincipalId`.

The script selects the target subscription explicitly. It does not run `az account set` or change your default subscription.

## Download

The first release is **v1.0.0**. Each ZIP contains one top-level `getpractical-sandboxes-lab` folder. From an empty working directory:

```powershell
Invoke-WebRequest `
    -Uri 'https://github.com/jpsantoscosta/getpractical-sandboxes-lab/releases/latest/download/getpractical-sandboxes-lab.zip' `
    -OutFile '.\getpractical-sandboxes-lab.zip'
Invoke-WebRequest `
    -Uri 'https://github.com/jpsantoscosta/getpractical-sandboxes-lab/releases/latest/download/SHA256SUMS' `
    -OutFile '.\SHA256SUMS'
$expected = ((Get-Content -LiteralPath '.\SHA256SUMS' -Raw).Trim() -split '\s+')[0]
$actual = (Get-FileHash -LiteralPath '.\getpractical-sandboxes-lab.zip' -Algorithm SHA256).Hash
if ($actual -ine $expected) { throw 'ZIP checksum mismatch.' }
Expand-Archive -LiteralPath '.\getpractical-sandboxes-lab.zip' -DestinationPath '.\'
Set-Location '.\getpractical-sandboxes-lab'
```

The checksum detects corruption; it is not a publisher signature. A normal clone of the repository works too. No file outside this repository or the downloaded folder is needed.

## Deploy and run

Run from the `getpractical-sandboxes-lab` folder. The commands in this section use live Azure; the [local checks](#local-checks) do not.

```powershell
.\lab.ps1 `
    -Action Deploy `
    -SubscriptionId '<subscription-guid>' `
    -Location 'westeurope' `
    -RunLab `
    -Samples 5
```

The script performs these steps:

1. It verifies the subscription, resource-provider registration, ARM API and region.
2. It saves a unique environment configuration under `.local\environment.json`.
3. It creates a local `.venv` and installs the pinned Python dependencies.
4. It compiles and deploys `infra\main.bicep`.
5. It grants the operator data-plane access at the sandbox-group scope.
6. It retries read-only data-plane preflight requests for up to three minutes while RBAC propagates.
7. It verifies the public image exists, then runs the experiments.

The script registers `Microsoft.App` if it is not already registered. It does not register `Microsoft.ContainerInstance` or request a preview feature flag.

To deploy without running the experiments, omit `-RunLab`. To run again:

```powershell
.\lab.ps1 -Action Run -Samples 5
```

To create a separate lab rather than reuse the current one:

```powershell
.\lab.ps1 `
    -Action Deploy `
    -SubscriptionId '<subscription-guid>' `
    -Location 'westeurope' `
    -LabName 'getpractical-storage-lab' `
    -StateFile '.local\second-environment.json' `
    -RunLab
```

Pass the same `-StateFile` to subsequent `Run` and `Destroy` commands. Repeating `Deploy` against the same state reuses the same resource group, sandbox group and deterministic role assignment. It does not create another environment. If a group with the requested name already exists without the matching local state, the script refuses to take it over.

For a CLI session authenticated as a service principal, use its Entra object ID, not its application/client ID:

```powershell
.\lab.ps1 `
    -Action Deploy `
    -SubscriptionId '<subscription-guid>' `
    -PrincipalId '<service-principal-object-guid>' `
    -PrincipalType ServicePrincipal `
    -Location 'westeurope' `
    -RunLab
```

The identity running the Python client must hold the assigned role, directly or through supported group membership. Assigning a different identity does not give the current operator access.

## Experiment 1. Separate local filesystems

The runner writes a random file to `/tmp/aca-lab/disk-token.txt` in sandbox A. A fresh sandbox B checks that the file does not exist in its own filesystem. The runner also records each guest's Linux boot ID.

Both checks must pass before B is deleted. These observations verify the test's local-file separation. They do not prove the hypervisor's security properties. The kernel version can be identical across separate machines.

Inspect the `fresh_sandboxes_do_not_share_local_files` and `fresh_boot_identifiers_differ` records.

## Experiment 2. Egress enforcement with independent evidence

`lab.py` submits this policy in every fresh sandbox's create request:

```json
{
  "defaultAction": "Deny",
  "trafficInspection": "Full",
  "rules": [
    {
      "name": "github-zen-get",
      "match": {
        "host": "api.github.com",
        "path": "/zen",
        "methods": ["GET"]
      },
      "action": {"type": "Allow"}
    }
  ]
}
```

The guest makes ordinary HTTPS requests with certificate validation enabled.

| Request | Required result |
| --- | --- |
| `GET https://api.github.com/zen` | HTTP 200 |
| `GET https://example.com/` | HTTP 403 plus a matching deny-audit entry |
| `GET https://api.github.com/` | HTTP 403 plus a matching deny-audit entry |

A timeout is not proof of a network-policy decision. The runner requires both an HTTP 403 and a matching audit record for the negative cases.

Audit reads did not always contain each individual request during development runs. The negative probes repeat only these safe GET requests, with a two-second delay and a 30-second observation deadline. Every response must remain 403. The report preserves the number of attempts and the response bodies. It fails if the audit evidence never appears, or if a request succeeds.

The runner records whether the successful request appeared in the audit read API. It does not assume that this API is a complete access log. A later [2026-09-01-preview REST specification](https://github.com/Azure/azure-rest-api-specs/blob/799f241aaa07e2485e0b06cc8f6f3b3caefd29da/specification/app/data-plane/ContainerApps/preview/2026-09-01-preview/containerappssandbox.json) describes the last 50 allowed and last 50 denied requests. This lab used the older `2026-02-01-preview` API, so it does not establish that version's retention contract. Missing entries in a read response do not prove service data loss or external telemetry-export loss. No external telemetry destination is configured or tested here.

The tests contact GitHub's public `/zen` endpoint and `example.com`. They transmit no user code, credentials or tenant data in request bodies.

## Experiment 3. Failed commands and bounded guest execution

The runner executes a Python process that exits with code 42. It checks that the SDK returns 42 instead of treating a successful HTTP response as a successful command.

It then runs:

```text
timeout 2s python3 -c 'import time; time.sleep(30)'
```

Exit code 124 means GNU `timeout` ended the test process. A later command must still run in the same sandbox.

This demonstrates command-result handling, not a hostile-code time limit. Code running as root can interfere with an in-guest supervisor. A production controller needs its own deadline and the authority to terminate the whole sandbox.

The SDK's automatic HTTP retries are disabled for the lab client. A failed or timed-out request to execute code is not necessarily safe to replay. The runner only adds an explicit retry around read-only RBAC preflight and the harmless network probes described above.

## Experiment 4. Memory restoration

`guest\state_probe.py` starts an HTTP server bound to loopback. The process generates a random nonce once at startup and keeps a counter in memory. It does not save the nonce or counter to a file.

The runner reads the process state, stops the sandbox in `Memory` mode, waits for a stopped state, resumes it, and waits for `Running`. A second loopback request must return the same nonce and boot ID, and the previous counter plus one.

The test does not use an arbitrary sleep to declare the sandbox ready. It waits for the service's state transitions. The loopback server has its own bounded readiness check.

This is stronger evidence than a file surviving suspension. Disk persistence alone cannot distinguish a memory restore from a normal reboot.

## Experiment 5. Point-in-time snapshot cloning

The runner captures a snapshot after the memory-resume test. It changes the source's disk marker afterward, then creates a new sandbox from the snapshot.

The clone must contain the older disk marker, the captured process nonce and the next counter value. These checks separate a point-in-time copy from a shared live filesystem.

The runner explicitly configures the clone's egress policy before repeating Experiment 2. This sequence is restricted to the trusted loopback fixture; it does not give arbitrary restored code a policy guarantee from its first instruction.

The pinned SDK rejects `labels`, `egress_policy`, ports, environment variables and several other overrides on a create-from-snapshot call. The lab uses the supported restore request rather than inventing an API option.

The controller stores returned clone IDs directly. Normal cleanup does not depend on inherited clone labels.

## Experiment 6. Disk-only suspension

The original sandbox switches to `Disk` mode and stops and resumes again. Its marker file must survive, but the loopback listener must be absent.

The runner starts the state server again. It must have a new process nonce and a new boot ID. This confirms that disk-only resume started a new guest execution rather than resuming the previous process memory.

## Experiment 7. Persistent Data Disk

The runner creates a 1 GiB `DataDisk` volume. A writer sandbox mounts it at `/mnt/lab`, writes a random marker, flushes the stream and calls `fsync`.

A second sandbox tries to mount the same volume concurrently. The operation must fail with a relevant HTTP 400 or 409 response, not with an authentication or unrelated error. The live run returned HTTP 409 with `VolumeAttached`.

The runner deletes the writer and waits for deletion to complete before creating a replacement. The replacement mounts the volume and must read the same marker. Finally, the runner deletes the replacement and the volume.

The shared root/Data Disk budget failure is an acknowledged regression with a smaller-root-disk workaround: [microsoft/azure-container-apps#1761](https://github.com/microsoft/azure-container-apps/issues/1761). A development run with 1 vCPU, a 20 GiB root disk and a 1 GiB Data Disk failed because 21 GiB exceeded the reported 20 GiB budget. The recorded successful profile uses 2 vCPU and 4 GiB memory while **keeping the root explicitly at 20 GiB**, including for the contender. This leaves room for the 1 GiB volume on that tested profile. Simply increasing CPU while letting the default root grow can still fail. This lab neither derives a general sizing formula from that error nor claims the regression is fixed for every creation path.

Data Disk attachments require `Disk` suspend mode. This test does not assert that a Blob volume provides the same POSIX or write-sharing semantics.

## Experiment 8. Measured startup

The runner creates and deletes five additional sandboxes sequentially. For each one it records:

| Measurement | Start and finish |
| --- | --- |
| `create_to_running_seconds` | Before the SDK create call to the poller returning `Running` |
| `first_exec_roundtrip_seconds` | After `Running` to a successful `printf sandbox-ready` response |
| `create_to_first_exec_seconds` | Before create to that first successful command |

These timings exclude ARM provisioning, dependency installation and image preparation. They include workstation-to-service latency and SDK overhead. The create poller uses a two-second interval when polling is needed; an initially running response can complete without that delay.

The report computes nearest-rank sample p50 and p95. With five samples, p95 is the maximum sample. It is not a production latency percentile or an SLA measurement.

Use `-Samples 20` for more observations. This runner remains sequential and is not a scale or quota load test.

## Historical measured results

The 25 September 2026 run recorded **all 21 checks passed**, successful runtime cleanup, 11 total sandbox creations, and five sequential startup samples. At most two sandboxes ran concurrently. Its roughly 127-second elapsed time includes the historical experiments and cleanup, not dependency installation or ARM deployment.

| Five-sample measurement | Sample p50 | Sample p95 |
| --- | --- | --- |
| Create to `Running` | 0.761 seconds | 0.789 seconds |
| Create to first successful exec | 0.832 seconds | 0.879 seconds |

| Single observation | Duration |
| --- | --- |
| Memory stop | 2.452 seconds |
| Memory resume | 0.460 seconds |
| Snapshot capture | 1.156 seconds |
| Clone create to first exec | 0.481 seconds |
| Disk stop | 1.466 seconds |
| Disk resume | 0.733 seconds |

The nonce and counter demonstrated process-memory continuity; the clone retained the checkpoint's earlier disk marker. Disk-only resume required a new process. The second simultaneous Data Disk attachment returned HTTP 409, and the file survived deleting the writer and attaching the volume to a replacement.

Read [evidence/results.json](evidence/results.json) for full-precision samples and provenance. These are measurements of one environment and one historical run, not performance guarantees, current-service certification or fresh measurements of this public revision.

## Inspect the evidence

Each run writes a separate directory under `.local\reports`.

```powershell
$latest = Get-ChildItem -LiteralPath '.local\reports' -Directory |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1

$report = Get-Content -LiteralPath (Join-Path $latest.FullName 'report.json') -Raw |
    ConvertFrom-Json

$report.status
$report.checks | Format-Table name, status
$report.benchmark | ConvertTo-Json -Depth 6
$report.cleanup_errors
```

`report.json` contains the results, measurements and observations. `resources.json` tracks resources that still need cleanup. The `egress-decisions-*.json` files preserve the request observations and corresponding audit reads.

A successful report has `status` equal to `passed`, all 21 checks passed, and an empty `cleanup_errors` array. Changing the benchmark sample count does not change the number of behavioral checks.

Raw reports can include generated resource IDs, local paths and service correlation IDs. Do not publish `.local` or `.venv`. Export a smaller summary with:

```powershell
.\.venv\Scripts\python.exe .\tools\export_results.py `
    --report (Join-Path $latest.FullName 'report.json') `
    --output '.\.local\public-results.json'
```

The exporter requires all 21 expected checks and successful cleanup, selects explicit fields, and removes raw check details and resource identifiers. It is not a general-purpose secret scrubber: review every exported value before sharing. Keep your export separate from the bundled historical evidence. Do not replace historical results or their source hash with invented or local-only test results.

## Cost and cleanup

Running sandboxes incur CPU and memory charges based on assigned resources. The script does not assume a Container Apps free grant applies. A stopped sandbox has no CPU or memory charge, but retained storage is a separate concern.

The service's pricing documentation on 25 September 2026 describes storage charging as coming soon. Check the current [sandbox cost page](https://sandboxes.azure.com/docs/sandboxes/cost) and your agreement before running. Do not treat that wording as a promise of permanent free storage.

The successful five-sample run took approximately 127 seconds including the experiments and cleanup. That elapsed duration is not a bill. The runner does not query Cost Management or calculate charged storage.

Normal and failed runs use `finally` cleanup. Creation labels help recover fresh sandboxes if a create operation fails after Azure accepted it. Explicit IDs cover completed snapshot clones. A machine crash, forced process termination or an ambiguous create response can still leave resources behind. Check the portal and local resource journal after an interrupted run.

To delete the entire dedicated lab, first read its exact name:

```powershell
$state = Get-Content -LiteralPath '.local\environment.json' -Raw | ConvertFrom-Json
$state.resource_group
```

Then confirm that name explicitly:

```powershell
.\lab.ps1 `
    -Action Destroy `
    -ConfirmResourceGroup $state.resource_group
```

The script checks its ownership tags and refuses group deletion if it finds unexpected ARM resources. It retains the local reports. Do not store anything you need inside this lab's sandbox group, because its group-scoped data is part of lab teardown.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Data-plane HTTP 403 | Group-scoped data-owner role, signed-in principal, target tenant and propagation. ARM Contributor does not replace the data-plane role |
| `InvalidResourceType` | Use `Microsoft.App/sandboxGroups@2026-07-01`; check the provider's advertised type, API and region |
| Image not found | Review the regional public-image catalog. The `python-3.12` alias is not an immutable image digest |
| HTTP 400 about total disk usage | Review [microsoft/azure-container-apps#1761](https://github.com/microsoft/azure-container-apps/issues/1761). Keep the root explicitly at 20 GiB with this lab's 2-vCPU/1-GiB-volume profile; do not infer a general sizing formula |
| HTTP 409 `VolumeAttached` | Wait for the previous sandbox's deletion. A volume is single-attach |
| A network negative test fails | Inspect both HTTP observations and audit files. Do not weaken the policy or disable TLS verification to pass |
| SDK `ValueError` for labels on snapshot creation | The restriction is in the pinned SDK; [microsoft/azure-container-apps#1810](https://github.com/microsoft/azure-container-apps/issues/1810) documents CLI support. Keep this lab's trusted-fixture restore path |
| HTTP 429 or core-quota error | Inspect the subscription's Sandbox Cores quota, reduce concurrency, and use bounded backoff in a production controller |
| Cleanup errors | Inspect `resources.json`, check the exact resources in the portal, and use the guarded teardown |

## Local checks

From a fresh checkout or extracted ZIP, create an isolated environment once:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\requirements.txt
.\.venv\Scripts\python.exe -m pip check
.\tools\check_local.ps1
```

If Bicep is missing, install it with `az bicep install` first. The helper runs `unittest`, Python syntax checks, PowerShell parsing, local Bicep compilation, and ZIP assembly. It **never invokes `Deploy`, `Run` or `Destroy`**, signs in to Azure, or uploads anything. Unit tests mock the SDK transport. No Azure account, secrets, guest endpoints or external telemetry services are needed. Installing dependencies and Bicep requires access to their download services.

Some unit tests deliberately print failed-check or cleanup-error messages, then assert that those failures are surfaced. The unittest result is the local test verdict. [CI](.github/workflows/ci.yml) performs the same checks on Windows with Python 3.12 and 3.14, including checks from an extracted ZIP, using a read-only token and no Azure authentication.

To check just the Python tests or build just the package:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s .\tests -v
python .\tools\package_release.py
```

Packaging uses only the Python standard library. The explicit public-file allowlist in [tools/package_release.py](tools/package_release.py) includes source, infrastructure, guest code, pins, documentation, license, local tooling, tests, CI and historical evidence. It never walks the workspace to decide what to publish. It rejects missing files and linked inputs, fixes ZIP metadata/order, and normalizes text to UTF-8/LF. It writes only ignored `dist\getpractical-sandboxes-lab.zip` and `dist\SHA256SUMS`. The archive excludes local state, raw reports, virtual environments, Git metadata, credentials, private documents and release notes. Rebuilds with the same inputs and Python/zlib produce the same ZIP bytes.

To verify the actual extracted distribution without duplicating the virtual environment:

```powershell
$python = (Resolve-Path '.\.venv\Scripts\python.exe').Path
Expand-Archive -LiteralPath '.\dist\getpractical-sandboxes-lab.zip' `
    -DestinationPath '.\.local\package-check'
& '.\.local\package-check\getpractical-sandboxes-lab\tools\check_local.ps1' -Python $python
```

Use a new empty extraction directory on subsequent runs; do not mix files from different releases.

## Maintaining a release

Keep the SDK and its compatible data-plane API paired. After intentional dependency or behavioral changes, update the related tests and README. A new live result requires a separately authorized Azure run; preserve the historical evidence unless replacing it with a reviewed, clearly dated export. Include new public files in the packaging allowlist deliberately.

For a future release, start from a **clean, reviewed main commit already pushed with CI passing**. Regenerate the ZIP, inspect its exact file list and contents, verify the extracted checks and SHA256, and write release notes to ignored `dist\release-notes-v1.0.0.md`. Notes should state the platform, costs, beta SDK pairing, scope of measurements and a link to this README.

The following commands **publish publicly**. Run them only after explicit publication approval; change the version and notes filename for later releases:

```powershell
.\tools\check_local.ps1
$version = 'v1.0.0'
$commit = git rev-parse HEAD
git tag -a $version $commit -m "GetPractical sandboxes lab $version"
git push origin $version
gh release create $version `
    '.\dist\getpractical-sandboxes-lab.zip' '.\dist\SHA256SUMS' `
    --repo 'jpsantoscosta/getpractical-sandboxes-lab' `
    --verify-tag --latest --title $version `
    --notes-file '.\dist\release-notes-v1.0.0.md'
```

Do not upload a workspace ZIP or overwrite an already published version. Keeping the asset name `getpractical-sandboxes-lab.zip` unchanged preserves the stable latest-download link. Publishing is manual; CI does not create tags, releases or uploads.

## Limitations and references

VNet egress, private ingress, managed-identity header injection, webhook authorization and external telemetry destinations need separate deployment and acceptance tests. They are not provisioned or verified here.

The lab's five-minute idle policy is configuration, not a demonstrated idle-to-suspend timing result. The stop and resume tests invoke those operations explicitly. There is no hidden 24-hour soak test behind the retention setting.

Official references: [Sandbox documentation](https://sandboxes.azure.com/docs/sandboxes/), [ARM/Bicep reference](https://learn.microsoft.com/en-us/azure/templates/microsoft.app/2026-07-01/sandboxgroups), [Python SDK 0.1.0b4](https://pypi.org/project/azure-containerapps-sandbox/0.1.0b4/), [Volumes](https://sandboxes.azure.com/docs/sandboxes/volumes), and [Cost](https://sandboxes.azure.com/docs/sandboxes/cost). Check current regional availability, quota, pricing and supported API/SDK combinations before a live run.
