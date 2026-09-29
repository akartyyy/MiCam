@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo  === Creando MiCam.exe ===
echo.
echo  Paso 1 de 2: instalando lo necesario...
python -m pip install --upgrade pyinstaller aiohttp pyvirtualcam opencv-python numpy cryptography av segno
if errorlevel 1 goto error
echo.
echo  Paso 2 de 2: armando el ejecutable (tarda un par de minutos)...
python -m PyInstaller --noconfirm --clean --onefile --windowed --name MiCam ^
  --icon micam.ico --add-data "micam.ico;." ^
  --collect-all pyvirtualcam --collect-all av micam.py
if errorlevel 1 goto error
echo.
echo  Listo. Tu programa esta en:  dist\MiCam.exe
echo.
explorer dist
pause
exit /b 0

:error
echo.
echo  Algo fallo. Copia el texto de arriba y mandaselo a Claude.
pause
exit /b 1
