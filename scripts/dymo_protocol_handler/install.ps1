# This installer maps the dymoprint:// protocol to the local PowerShell script.

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$targetScript = "$scriptDir\print_dymo.ps1"

if (-not (Test-Path $targetScript)) {
    Write-Error "Could not find print_dymo.ps1 in the same directory!"
    Pause
    Exit
}

Write-Host "Registering the dymoprint:// protocol to bypass Chrome Security..."

# We use HKCU Software Classes so it doesn't require hard Administrator privileges
# and avoids the "HKCR Drive Not Found" error on some PowerShell setups.
$path = "HKCU:\Software\Classes\dymoprint"

# Create Protocol Keys in Registry
New-Item -Path $path -Force | Out-Null
Set-ItemProperty -Path $path -Name "(Default)" -Value "URL:RSCP DYMO Print Protocol"
Set-ItemProperty -Path $path -Name "URL Protocol" -Value ""

# Define Command hook
$cmdPath = "$path\shell\open\command"
New-Item -Path $cmdPath -Force | Out-Null

# We use -WindowStyle Hidden so it runs silently in the background when the user clicks 'Print'
$powershellPath = "powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -NoProfile -File `"$targetScript`" `"%1`""
Set-ItemProperty -Path $cmdPath -Name "(Default)" -Value $powershellPath

Write-Host "Success!" -ForegroundColor Green
Write-Host "The exact script it is mapped to is:" -ForegroundColor Cyan
Write-Host $targetScript
Write-Host ""
Write-Host "You can now click 'Print' inside RSCP, and Chrome will seamlessly pass the request to this bridge file."
Write-Host "Press any key to close this window..."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
