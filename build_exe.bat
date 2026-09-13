@echo off
REM build_exe.bat
REM Builds video_clipper_gui.py into a standalone Windows .exe (no Python needed to run it)
REM AND downloads ffmpeg.exe/ffprobe.exe to sit right next to it, so the whole
REM "dist" folder is fully self-contained - no separate ffmpeg install needed.
REM
REM HOW TO USE:
REM   1. Make sure Python is installed (python.org) and "python" works in a terminal.
REM   2. Put this file in the SAME folder as video_clipper_gui.py.
REM   3. Double-click this file (or run it from a terminal).
REM   4. Wait for it to finish - everything you need is in the "dist" folder.
REM   5. Copy the whole "dist" folder wherever you like; double-click VideoClipper.exe.

echo ============================================
echo  Video Clipper - EXE Builder
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python was not found on PATH.
    echo Install it from https://www.python.org/downloads/ and check
    echo "Add python.exe to PATH" during install, then run this again.
    pause
    exit /b 1
)

echo Installing/upgrading PyInstaller and yt-dlp...
python -m pip install --upgrade pyinstaller yt-dlp pycryptodomex tkinterdnd2 customtkinter Pillow
if errorlevel 1 (
    echo ERROR: Failed to install dependencies. Check your internet connection.
    pause
    exit /b 1
)

echo.
echo Building the .exe (this can take a minute or two)...
python -m PyInstaller --onefile --windowed --name "VideoClipper" ^
  --collect-all yt_dlp ^
  --collect-all certifi ^
  --collect-all Cryptodome ^
  --collect-all tkinterdnd2 ^
  --collect-all customtkinter ^
  --collect-all PIL ^
  video_clipper_gui.py

if errorlevel 1 (
    echo.
    echo ERROR: Build failed. Scroll up to see what went wrong.
    pause
    exit /b 1
)

echo.
echo Downloading ffmpeg (this bundles ffmpeg.exe / ffprobe.exe next to your app)...

set FFMPEG_PS1=%TEMP%\ffmpeg_download_step.ps1
> "%FFMPEG_PS1%" (
    echo $ErrorActionPreference = 'Stop'
    echo $zip = Join-Path $env:TEMP 'ffmpeg_download.zip'
    echo Invoke-WebRequest -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile $zip
    echo $extractDir = Join-Path $env:TEMP 'ffmpeg_extract'
    echo if ^(Test-Path $extractDir^) { Remove-Item $extractDir -Recurse -Force }
    echo Expand-Archive -Path $zip -DestinationPath $extractDir -Force
    echo $bin = Get-ChildItem -Path $extractDir -Recurse -Filter 'ffmpeg.exe' ^| Select-Object -First 1 ^| ForEach-Object { $_.DirectoryName }
    echo Copy-Item ^(Join-Path $bin 'ffmpeg.exe'^) 'dist\ffmpeg.exe' -Force
    echo Copy-Item ^(Join-Path $bin 'ffprobe.exe'^) 'dist\ffprobe.exe' -Force
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%FFMPEG_PS1%"
del "%FFMPEG_PS1%" 2>nul

if errorlevel 1 (
    echo.
    echo WARNING: Automatic ffmpeg download failed ^(maybe no internet access^).
    echo You can still download it yourself from https://ffmpeg.org/download.html
    echo and place ffmpeg.exe and ffprobe.exe directly inside the "dist" folder.
    echo.
    pause
    exit /b 0
)

echo.
echo ============================================
echo  Done! Everything is in the "dist" folder:
echo    dist\VideoClipper.exe
echo    dist\ffmpeg.exe
echo    dist\ffprobe.exe
echo ============================================
echo.
echo Copy the whole "dist" folder anywhere you like and double-click
echo VideoClipper.exe to run it - no separate installs needed.
echo.
echo NOTE: URL downloading depends on yt-dlp, which YouTube can break with
echo site changes from time to time. If URL mode stops working later, just
echo re-run this build script to pull in the latest yt-dlp and rebuild.
echo.
pause
