@echo off
setlocal enabledelayedexpansion

REM ============================================================
REM  For MULTIPLE .tif images in one unattended run. For a single
REM  image, use run_pipeline.bat instead, it is simpler.
REM
REM  List every site in sites.csv, next to this script, one line
REM  each: site_name,C:\full\path\to\image.tif
REM  See sites.csv.example for a template and the exact format.
REM  Each image is read straight from that path, nothing is copied.
REM
REM  PRIORITY: how much this script competes with other work on
REM  this machine for CPU time. Three choices:
REM    low         - never competes. Only uses CPU that nothing
REM                  else wants, so it slows down or nearly pauses
REM                  while other work is busy, and speeds back up
REM                  once it is free again. (default)
REM    belownormal - still yields to other work, but gets a bigger
REM                  share of CPU while doing so, use this if "low"
REM                  barely progresses.
REM    normal      - does not yield at all, this script competes
REM                  for CPU exactly like any other ordinary program
REM                  on the machine, fastest but can slow down
REM                  whatever else is running.
REM ============================================================
set MANIFEST=sites.csv
set PRIORITY=low

REM Run from the folder this .bat file is in (must be the repo
REM root, alongside config.yaml, src\ and scripts\)
cd /d "%~dp0"

if not exist "%MANIFEST%" (
    echo Cannot find %MANIFEST% next to this script.
    echo Copy sites.csv.example to sites.csv and fill in your real
    echo site names and image paths first.
    pause
    exit /b 1
)

set PY=.venv\Scripts\python.exe
set SUMMARY_LOG=run_batch_summary.log

echo ================================================ > "%SUMMARY_LOG%"
echo Batch run started at: %date% %time% >> "%SUMMARY_LOG%"
echo Manifest: %MANIFEST% >> "%SUMMARY_LOG%"
echo Priority: %PRIORITY% >> "%SUMMARY_LOG%"
echo ================================================ >> "%SUMMARY_LOG%"

REM One site failing does not stop the batch, the remaining sites
REM still run. Each site's own detailed log is run_<site>.log, same
REM as run_pipeline.bat; this script also writes a short summary
REM line per site to run_batch_summary.log.
REM
REM "start ... /%PRIORITY%" launches PowerShell itself at that
REM priority class; Windows gives a new process its creator's
REM priority by default when that creator is "low" or "belownormal"
REM (not "normal"), so python.exe, started from inside that
REM PowerShell, inherits the same low priority automatically.

for /f "usebackq tokens=1,2 delims=," %%A in ("%MANIFEST%") do (
    set "LINE=%%A"
    if not "!LINE!"=="" if not "!LINE:~0,1!"=="#" (
        call :run_one "%%A" "%%B"
    )
)

echo.
echo ================================================
echo Batch finished. Summary below, also saved to %SUMMARY_LOG%.
echo Each site's full detail is in its own run_^<site^>.log.
echo ================================================
type "%SUMMARY_LOG%"
echo ================================================ >> "%SUMMARY_LOG%"
echo Batch finished at: %date% %time% >> "%SUMMARY_LOG%"
goto :end

:run_one
set "SITE=%~1"
set "ORTHO=%~2"
set "LOG=run_%SITE%.log"

echo.
echo ================================================
echo Site: %SITE%
echo ================================================
echo ================================================ > "%LOG%"
echo Pipeline for site: %SITE% >> "%LOG%"
echo Source image: %ORTHO% >> "%LOG%"
echo Started at: %date% %time% >> "%LOG%"
echo ================================================ >> "%LOG%"

if not exist "%ORTHO%" (
    echo Cannot find the image for %SITE%: "%ORTHO%"
    echo Check the path in %MANIFEST% and that its drive is connected.
    echo Cannot find ORTHO: "%ORTHO%" >> "%LOG%"
    goto :site_failed
)

echo [1/5] Cutting the image into tiles...
start "" /%PRIORITY% /b /wait powershell -NoProfile -Command "& '%PY%' src\tiling.py --config config.yaml --ortho '%ORTHO%' --site '%SITE%' 2>&1 | ForEach-Object { $_; Add-Content -LiteralPath '%LOG%' -Value $_ -Encoding ascii }; exit $LASTEXITCODE"
if errorlevel 1 goto :site_failed

echo [2/5] Extracting DINOv2 features...
start "" /%PRIORITY% /b /wait powershell -NoProfile -Command "& '%PY%' src\features.py --config config.yaml --site '%SITE%' 2>&1 | ForEach-Object { $_; Add-Content -LiteralPath '%LOG%' -Value $_ -Encoding ascii }; exit $LASTEXITCODE"
if errorlevel 1 goto :site_failed

echo [3/5] Building the feature cache...
start "" /%PRIORITY% /b /wait powershell -NoProfile -Command "& '%PY%' src\propagate.py --config config.yaml --site '%SITE%' --build-feature-cache --ortho '%ORTHO%' 2>&1 | ForEach-Object { $_; Add-Content -LiteralPath '%LOG%' -Value $_ -Encoding ascii }; exit $LASTEXITCODE"
if errorlevel 1 goto :site_failed

echo [4/5] Building the SAM candidate cache (the slowest step)...
start "" /%PRIORITY% /b /wait powershell -NoProfile -Command "& '%PY%' src\propagate.py --config config.yaml --site '%SITE%' --build-sam-cache --ortho '%ORTHO%' 2>&1 | ForEach-Object { $_; Add-Content -LiteralPath '%LOG%' -Value $_ -Encoding ascii }; exit $LASTEXITCODE"
if errorlevel 1 goto :site_failed

echo [5/5] Creating the species label layers...
start "" /%PRIORITY% /b /wait powershell -NoProfile -Command "& '%PY%' scripts\init_labels_gpkg.py --config config.yaml --site '%SITE%' --ortho '%ORTHO%' 2>&1 | ForEach-Object { $_; Add-Content -LiteralPath '%LOG%' -Value $_ -Encoding ascii }; exit $LASTEXITCODE"
if errorlevel 1 goto :site_failed

echo   -^> done
echo %SITE%: DONE, log: %LOG% >> "%SUMMARY_LOG%"
exit /b 0

:site_failed
echo   -^> FAILED, see %LOG%
echo %SITE%: FAILED, see %LOG% >> "%SUMMARY_LOG%"
exit /b 0

:end
pause
