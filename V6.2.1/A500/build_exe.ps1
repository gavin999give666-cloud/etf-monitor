# Build ParamOptimizerUI.exe (standalone optimizer + control panel GUI)
# Usage: run in A500 folder:  powershell -ExecutionPolicy Bypass -File build_exe.ps1
# Output: dist\ParamOptimizerUI.exe
#
# Note: put the EXE next to stock_data.db (e.g., in the A500 folder) before running,
#       otherwise market data cannot be loaded.

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

Write-Host '==> Building ParamOptimizerUI.exe ...' -ForegroundColor Cyan

# Exclude optional heavy deps (optimizer only reads the local DB;
# akshare/baostock are lazy fallback data sources in data_updater;
# sklearn/scipy/torch/matplotlib are optional ML enhancements with graceful fallback).
$pyArgs = @(
    '--noconfirm', '--clean', '--onefile', '--console',
    '--name', 'ParamOptimizerUI',
    '--exclude-module', 'akshare',
    '--exclude-module', 'baostock',
    '--exclude-module', 'sklearn',
    '--exclude-module', 'scipy',
    '--exclude-module', 'torch',
    '--exclude-module', 'torchvision',
    '--exclude-module', 'matplotlib',
    '--hidden-import', 'optimizer_gui',
    '--hidden-import', 'adaptive_pool',
    '--hidden-import', 'optimizer_modes',
    '--hidden-import', 'dpi_utils',
    'optimizer_ui_main.py'
)

python -m PyInstaller @pyArgs

if (Test-Path "dist\ParamOptimizerUI.exe") {
    Write-Host "`nBuild OK: $PSScriptRoot\dist\ParamOptimizerUI.exe" -ForegroundColor Green
    Write-Host 'Tip: copy the EXE next to stock_data.db (e.g., A500 folder) and double-click it.' -ForegroundColor Yellow
} else {
    Write-Host 'Build FAILED, check errors above.' -ForegroundColor Red
}
