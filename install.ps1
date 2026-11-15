# toranime installer for Windows:
#   irm https://raw.githubusercontent.com/throwin5s/toranime/main/install.ps1 | iex
# installs uv (which fetches its own Python if you have none), then toranime, then mpv.
# re-run it any time to update.
#
# env overrides (used by CI):
#   TORANIME_PKG=<path, url or requirement>   install this instead of the latest main
#   TORANIME_YES=1                            install mpv without asking
& {
    # judge every step by its exit code. "Stop" would make Windows PowerShell 5.1 treat
    # uv's normal progress output on stderr as fatal whenever output is redirected
    $ErrorActionPreference = "Continue"
    $pkg = "toranime-mpv @ https://github.com/throwin5s/toranime/archive/refs/heads/main.zip"
    if ($env:TORANIME_PKG) { $pkg = $env:TORANIME_PKG }

    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        powershell -NoProfile -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
        if ($LASTEXITCODE) { throw "uv install failed" }
    }
    $env:Path = (Join-Path $env:USERPROFILE ".local\bin") + ";" + $env:Path

    uv tool install --force --reinstall $pkg
    if ($LASTEXITCODE) { throw "toranime install failed" }

    if ($env:TORANIME_YES) { toranime --setup --yes } else { toranime --setup }
    if ($LASTEXITCODE) { throw "mpv setup failed" }
    Write-Host ""
    Write-Host "done. run: toranime   (open a new terminal first if the command is not found)"
}
