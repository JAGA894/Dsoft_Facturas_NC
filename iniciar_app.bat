@echo off
REM ==========================================================================
REM  iniciar_app.bat - Inicia la aplicacion web en el puerto 8501
REM  Acceso: http://localhost:8501  o  http://<IP-DEL-SERVIDOR>:8501
REM ==========================================================================
chcp 65001 >nul
cd /d "%~dp0"

if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
) else (
    echo [AVISO] No existe el entorno virtual. Ejecuta primero instalar.bat
)

if not exist .env (
    echo [ERROR] Falta el archivo .env con las credenciales de SAP.
    pause
    exit /b 1
)

echo Iniciando DSOFT - Carga a SAP B1 en http://localhost:8501
echo (No cierres esta ventana mientras la aplicacion este en uso)
python -m streamlit run app.py --server.port 8501 --server.address 0.0.0.0
