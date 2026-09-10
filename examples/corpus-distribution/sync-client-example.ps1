# PREPARED, NOT VERIFIED — no real Windows environment ran this script this
# round. It is the client-side sync entry for native PowerShell 5.1/7 and
# exists so the Windows validation batch has a concrete starting point.
#
# What it does: resolve the pinned knowledge venv, then run one periodic
# distribution check against a local release mirror directory. The Python
# module owns locking, staging, verification, import and the atomic switch;
# this wrapper only passes paths through. The module never overwrites an open
# database in place (each version is a new URI subtree) and never uses
# POSIX-only flock/fork.
#
# Example:
#   powershell -File sync-client-example.ps1 `
#     -StateRoot "$env:USERPROFILE\.vaws-local\knowledge" `
#     -SourceDir D:\mirror\knowledge-release

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$StateRoot,
    [Parameter(Mandatory = $true)][string]$SourceDir,
    [string]$Python = "python",
    [string]$OpenVikingUrl = "",
    [string]$EmbeddingHealthUrl = ""
)

$ErrorActionPreference = "Stop"

$arguments = @(
    "-m", "vaws_knowledge.distribution", "check",
    "--state-root", $StateRoot,
    "--source", $SourceDir
)
if ($OpenVikingUrl) { $arguments += @("--openviking-url", $OpenVikingUrl) }
if ($EmbeddingHealthUrl) { $arguments += @("--embedding-health-url", $EmbeddingHealthUrl) }

& $Python @arguments
if ($LASTEXITCODE -eq 0) { exit 0 }
# Statuses busy/offline/corrupt/incompatible all keep the old version active;
# the reason is in the JSON the module prints. Let the next period retry.
exit $LASTEXITCODE
