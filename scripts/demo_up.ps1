[CmdletBinding()]
param(
    [string]$PythonPath = "python",
    [switch]$SkipInstall,
    [int]$SignalDays = 1,
    [switch]$LiveModel,
    [switch]$OfflineAi,
    [switch]$OfflineData,
    [switch]$WithoutMl,
    [switch]$CheckOnly,
    [int]$Port = 8000
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
# Delegate to the canonical launcher so Gemini checks, isolation and assets
# behave identically on Windows and macOS. Dependencies must be installed in
# the selected Python environment; SkipInstall remains a compatibility flag.
if ($SignalDays -ne 1) {
    throw "The judge demo uses one reference signal day to preserve the curated baseline."
}
if ($LiveModel -and $OfflineAi) {
    throw "Choose live Gemini or explicit offline AI, not both."
}
$launcher = Join-Path -Path $PSScriptRoot -ChildPath "demo_up.py"
$arguments = @($launcher, "--port", "$Port")
if ($LiveModel) { $arguments += "--live-model" }
if ($OfflineAi) { $arguments += "--offline-ai" }
if ($OfflineData) { $arguments += "--offline-data" }
if ($WithoutMl) { $arguments += "--without-ml" }
if ($CheckOnly) { $arguments += "--check-only" }
& $PythonPath @arguments
exit $LASTEXITCODE
