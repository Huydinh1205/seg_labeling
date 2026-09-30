@echo off
setlocal

REM ============================================================
REM  Sua 2 dong duoi day truoc khi chay:
REM  - SITE: ten site (dat tuy y, dung de dat ten thu muc/file)
REM  - ORTHO: duong dan DAY DU den file anh .tif, o bat ky dau
REM           trong may cung duoc, khong bat buoc phai o data\raw
REM  Vi du:
REM    set SITE=walpolla_island
REM    set ORTHO=D:\Drone Data\2026\walpolla_ortho.tif
REM ============================================================
set SITE=your_site_name
set ORTHO=C:\duong\dan\day\du\den\anh.tif
REM ============================================================

REM Chay tu chinh thu muc chua file .bat nay (phai la thu muc goc
REM cua repo, cung cap voi config.yaml, src\ va scripts\)
cd /d "%~dp0"

set PY=.venv\Scripts\python.exe
set LOG=run_%SITE%.log

echo ================================================ > "%LOG%"
echo Pipeline cho site: %SITE% >> "%LOG%"
echo Anh nguon: %ORTHO% >> "%LOG%"
echo Bat dau luc: %date% %time% >> "%LOG%"
echo ================================================ >> "%LOG%"

REM Moi buoc chay qua PowerShell + Tee-Object de vua in ra man hinh
REM ngay luc dang chay (theo doi duoc tien do, bao gom ca progress
REM bar cua tung script neu no co in), vua ghi lai vao file log.
REM Day khong phai chay script .ps1 nen khong bi chan boi execution
REM policy tren may cong ty.

echo.
echo [1/5] (khoang 0%% - 10%%) Cat anh thanh tile...
powershell -NoProfile -Command "& '%PY%' src\tiling.py --config config.yaml --ortho '%ORTHO%' 2>&1 | Tee-Object -FilePath '%LOG%' -Append; exit $LASTEXITCODE"
if errorlevel 1 goto :error

echo.
echo [2/5] (khoang 10%% - 35%%) Trich dac trung DINOv2...
powershell -NoProfile -Command "& '%PY%' src\features.py --config config.yaml --site '%SITE%' 2>&1 | Tee-Object -FilePath '%LOG%' -Append; exit $LASTEXITCODE"
if errorlevel 1 goto :error

echo.
echo [3/5] (khoang 35%% - 40%%) Dung feature cache...
powershell -NoProfile -Command "& '%PY%' src\propagate.py --config config.yaml --site '%SITE%' --build-feature-cache 2>&1 | Tee-Object -FilePath '%LOG%' -Append; exit $LASTEXITCODE"
if errorlevel 1 goto :error

echo.
echo [4/5] (khoang 40%% - 95%%, buoc lau nhat) Dung SAM candidate cache...
powershell -NoProfile -Command "& '%PY%' src\propagate.py --config config.yaml --site '%SITE%' --build-sam-cache 2>&1 | Tee-Object -FilePath '%LOG%' -Append; exit $LASTEXITCODE"
if errorlevel 1 goto :error

echo.
echo [5/5] (khoang 95%% - 100%%) Tao layer nhan theo loai...
powershell -NoProfile -Command "& '%PY%' scripts\init_labels_gpkg.py --config config.yaml --site '%SITE%' 2>&1 | Tee-Object -FilePath '%LOG%' -Append; exit $LASTEXITCODE"
if errorlevel 1 goto :error

echo.
echo ================================================
echo XONG 100%%. Site %SITE% da san sang, mo QGIS va chay repair_session.py la duoc.
echo Log chi tiet: %LOG%
echo ================================================
echo Ket thuc thanh cong luc: %date% %time% >> "%LOG%"
goto :end

:error
echo.
echo ================================================
echo LOI o buoc vua roi. Mo file %LOG% de xem chi tiet loi.
echo Cac buoc sau se KHONG chay vi buoc nay chua xong.
echo ================================================
echo THAT BAI luc: %date% %time% >> "%LOG%"

:end
pause
