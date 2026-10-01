@echo off
setlocal

REM ============================================================
REM  Edit the lines below before running:
REM  - SITE: the site name (any name you like, used for folder/file naming)
REM  - ORTHO: the FULL path to the .tif image, anywhere on this
REM           machine, it does not have to be under data\raw
REM  - PRIORITY: how much this script yields to other work on this
REM           machine. "low" (default) means it only uses CPU the
REM           other tasks are not using right now, so it will run
REM           SLOWER whenever something else is busy, but never
REM           competes with it. Use "belownormal" instead if "low"
REM           makes it barely progress while other work is running.
REM  Example:
REM    set SITE=walpolla_island
REM    set ORTHO=D:\Drone Data\2026\walpolla_ortho.tif
REM ============================================================
set SITE=your_site_name
set ORTHO=C:\full\path\to\image.tif
set PRIORITY=low
REM ============================================================

REM Run from the folder this .bat file is in (must be the repo
REM root, alongside config.yaml, src\ and scripts\)
cd /d "%~dp0"

set PY=.venv\Scripts\python.exe
set LOG=run_%SITE%.log

echo ================================================ > "%LOG%"
echo Pipeline for site: %SITE% >> "%LOG%"
echo Source image: %ORTHO% >> "%LOG%"
echo Priority: %PRIORITY% >> "%LOG%"
echo Started at: %date% %time% >> "%LOG%"
echo ================================================ >> "%LOG%"

REM Each step runs through PowerShell + Tee-Object, which prints
REM to the console live while it runs (so progress, including any
REM progress bar a script prints, is visible as it happens) and
REM also appends the same output to the log file. This never runs
REM a .ps1 script file, so it is not blocked by a company machine's
REM PowerShell execution policy.
REM
REM "start ... /%PRIORITY%" launches PowerShell itself at that
REM priority class; Windows gives a new process its creator's
REM priority by default when that creator is "low" or "belownormal"
REM (not "normal"), so python.exe, started from inside that
REM PowerShell, inherits the same low priority automatically.

echo.
echo [1/5] (roughly 0%% - 10%%) Cutting the image into tiles...
start "" /%PRIORITY% /b /wait powershell -NoProfile -Command "& '%PY%' src\tiling.py --config config.yaml --ortho '%ORTHO%' 2>&1 | Tee-Object -FilePath '%LOG%' -Append; exit $LASTEXITCODE"
if errorlevel 1 goto :error

echo.
echo [2/5] (roughly 10%% - 35%%) Extracting DINOv2 features...
start "" /%PRIORITY% /b /wait powershell -NoProfile -Command "& '%PY%' src\features.py --config config.yaml --site '%SITE%' 2>&1 | Tee-Object -FilePath '%LOG%' -Append; exit $LASTEXITCODE"
if errorlevel 1 goto :error

echo.
echo [3/5] (roughly 35%% - 40%%) Building the feature cache...
start "" /%PRIORITY% /b /wait powershell -NoProfile -Command "& '%PY%' src\propagate.py --config config.yaml --site '%SITE%' --build-feature-cache 2>&1 | Tee-Object -FilePath '%LOG%' -Append; exit $LASTEXITCODE"
if errorlevel 1 goto :error

echo.
echo [4/5] (roughly 40%% - 95%%, the slowest step) Building the SAM candidate cache...
start "" /%PRIORITY% /b /wait powershell -NoProfile -Command "& '%PY%' src\propagate.py --config config.yaml --site '%SITE%' --build-sam-cache 2>&1 | Tee-Object -FilePath '%LOG%' -Append; exit $LASTEXITCODE"
if errorlevel 1 goto :error

echo.
echo [5/5] (roughly 95%% - 100%%) Creating the species label layers...
start "" /%PRIORITY% /b /wait powershell -NoProfile -Command "& '%PY%' scripts\init_labels_gpkg.py --config config.yaml --site '%SITE%' 2>&1 | Tee-Object -FilePath '%LOG%' -Append; exit $LASTEXITCODE"
if errorlevel 1 goto :error

echo.
echo ================================================
echo DONE (100%%). Site %SITE% is ready, open QGIS and run repair_session.py.
echo Full log: %LOG%
echo ================================================
echo Finished successfully at: %date% %time% >> "%LOG%"
goto :end

:error
echo.
echo ================================================
echo FAILED at the step above. Open %LOG% for the error details.
echo The remaining steps did NOT run because this one did not finish.
echo ================================================
echo FAILED at: %date% %time% >> "%LOG%"

:end
pause
