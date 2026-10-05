@echo off
REM ==========================================================================
REM  instalar.bat - Instalacion inicial (ejecutar UNA sola vez en el servidor)
REM  Crea el entorno virtual, instala dependencias y prepara el archivo .env
REM ==========================================================================
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo === 1/4 Verificando Python ===
python --version
if errorlevel 1 (
    echo [ERROR] Python no esta instalado o no esta en el PATH.
    echo Instala Python desde https://www.python.org/downloads/ marcando "Add python.exe to PATH".
    pause
    exit /b 1
)

echo.
echo === 2/4 Creando entorno virtual (venv) ===
if not exist venv (
    python -m venv venv
) else (
    echo venv ya existe, se reutiliza.
)

echo.
echo === 3/4 Instalando dependencias ===
call venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Fallo la instalacion de dependencias. Revisa tu conexion a internet.
    pause
    exit /b 1
)

echo.
echo === 4/4 Archivo de configuracion .env ===
if not exist .env (
    copy .env.example .env >nul
    echo Se creo .env a partir de .env.example
    echo IMPORTANTE: abre .env con el Bloc de notas y llena tus datos de SAP.
    notepad .env
) else (
    echo .env ya existe, no se modifica.
)

echo.
echo === Verificando conexion con SAP ===
python verificar_sap.py

echo.
echo Instalacion terminada. Para iniciar la aplicacion ejecuta: iniciar_app.bat
pause
