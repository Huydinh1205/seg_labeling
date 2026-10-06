@echo off
setlocal

REM ============================================================
REM  Edit the lines below before running:
REM  - SITE: the site name (any name you like, used for folder/file naming)
REM  - ORTHO: the FULL path to the original .tif image, anywhere on
REM           this machine. Quotes around the path are optional. Step 0
REM           makes a projected (UTM, metres) copy at the working
REM           resolution set in config.yaml -> prepare, under
REM           data\prepared\, and every later step and the QGIS session
REM           use that copy. The original is never modified.
REM  - PRIORITY: how much this script competes with other work on
REM           this machine for CPU time. Three choices:
REM             low         - never competes. Only uses CPU that
REM                           nothing else wants, so it slows down
REM                           or nearly pauses while other work is
REM                           busy, and speeds back up once it is
REM                           free again. (default)
REM             belownormal - still yields to other work, but gets
REM                           a bigger share of CPU while doing so,
REM                           use this if "low" barely progresses.
REM             normal      - does not yield at all, this script
REM                           competes for CPU exactly like any
REM                           other ordinary program on the machine,
REM                           which is the fastest option but can
REM                           slow down whatever else is running.
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

REM Strip any double quotes typed around SITE or ORTHO above. Writing
REM set ORTHO="C:\path with spaces\image.tif" is a natural habit, but
REM every command below already adds its own quotes, and doubled
REM quotes break any path that contains a space.
set SITE=%SITE:"=%
set ORTHO=%ORTHO:"=%

set PY=.venv\Scripts\python.exe
set LOG=run_%SITE%.log

echo ================================================ > "%LOG%"
echo Pipeline for site: %SITE% >> "%LOG%"
echo Source image: %ORTHO% >> "%LOG%"
echo Priority: %PRIORITY% >> "%LOG%"
echo Started at: %date% %time% >> "%LOG%"
echo ================================================ >> "%LOG%"

REM Each step runs through PowerShell, which prints to the console
REM live while it runs (so progress, including any progress bar a
REM script prints, is visible as it happens) and also appends the
REM same output to the log file. The append goes through
REM Add-Content -Encoding ascii rather than Tee-Object's own file
REM write, because Tee-Object defaults to UTF-16 on Windows
REM PowerShell, which does not match the plain ASCII this script
REM itself writes above, and a log file mixing both encodings opens
REM as garbled or looks empty in Notepad. This never runs a .ps1
REM script file, so it is not blocked by a company machine's
REM PowerShell execution policy.
REM
REM "start ... /%PRIORITY%" launches PowerShell itself at that
REM priority class; Windows gives a new process its creator's
REM priority by default when that creator is "low" or "belownormal"
REM (not "normal"), so python.exe, started from inside that
REM PowerShell, inherits the same low priority automatically.

if not exist "%ORTHO%" (
    echo.
    echo Cannot find the image set in ORTHO: "%ORTHO%"
    echo Check the path is right and that the drive it is on is connected.
    echo Cannot find ORTHO: "%ORTHO%" >> "%LOG%"
    goto :error
)

echo.
echo [0/5] Preparing the orthomosaic ^(UTM metres, working resolution from config.yaml^)...
start "" /%PRIORITY% /b /wait powershell -NoProfile -Command "& '%PY%' scripts\prepare_ortho.py --config config.yaml --site '%SITE%' --ortho '%ORTHO%' 2>&1 | ForEach-Object { $_; Add-Content -LiteralPath '%LOG%' -Value $_ -Encoding ascii }; exit $LASTEXITCODE"
if errorlevel 1 goto :error
set /p PREP=<"data\prepared\%SITE%.path.txt"
echo       using: %PREP%
echo Prepared image: %PREP% >> "%LOG%"

echo.
echo [1/5] (roughly 0%% - 10%%) Cutting the image into tiles...
start "" /%PRIORITY% /b /wait powershell -NoProfile -Command "& '%PY%' src\tiling.py --config config.yaml --ortho '%PREP%' --site '%SITE%' 2>&1 | ForEach-Object { $_; Add-Content -LiteralPath '%LOG%' -Value $_ -Encoding ascii }; exit $LASTEXITCODE"
if errorlevel 1 goto :error

echo.
echo [2/5] (roughly 10%% - 35%%) Extracting DINOv2 features...
start "" /%PRIORITY% /b /wait powershell -NoProfile -Command "& '%PY%' src\features.py --config config.yaml --site '%SITE%' 2>&1 | ForEach-Object { $_; Add-Content -LiteralPath '%LOG%' -Value $_ -Encoding ascii }; exit $LASTEXITCODE"
if errorlevel 1 goto :error

echo.
echo [3/5] (roughly 35%% - 40%%) Building the feature cache...
start "" /%PRIORITY% /b /wait powershell -NoProfile -Command "& '%PY%' src\propagate.py --config config.yaml --site '%SITE%' --build-feature-cache --ortho '%PREP%' 2>&1 | ForEach-Object { $_; Add-Content -LiteralPath '%LOG%' -Value $_ -Encoding ascii }; exit $LASTEXITCODE"
if errorlevel 1 goto :error

echo.
echo [4/5] (roughly 40%% - 95%%, the slowest step) Building the SAM candidate cache...
start "" /%PRIORITY% /b /wait powershell -NoProfile -Command "& '%PY%' src\propagate.py --config config.yaml --site '%SITE%' --build-sam-cache --ortho '%PREP%' 2>&1 | ForEach-Object { $_; Add-Content -LiteralPath '%LOG%' -Value $_ -Encoding ascii }; exit $LASTEXITCODE"
if errorlevel 1 goto :error

echo.
echo [5/5] (roughly 95%% - 100%%) Creating the species label layers...
start "" /%PRIORITY% /b /wait powershell -NoProfile -Command "& '%PY%' scripts\init_labels_gpkg.py --config config.yaml --site '%SITE%' --ortho '%PREP%' 2>&1 | ForEach-Object { $_; Add-Content -LiteralPath '%LOG%' -Value $_ -Encoding ascii }; exit $LASTEXITCODE"
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
