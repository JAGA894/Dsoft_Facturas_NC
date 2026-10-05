# DSOFT – Carga de Facturas SAT a SAP B1

Aplicación web interna (Streamlit) que lee el reporte de facturas del SAT (CSV/XLSX), valida la empresa por RFC, transforma los datos con pandas y los carga (upsert) a la tabla de usuario **@FACTURAS** de SAP Business One mediante **Service Layer**, con trazabilidad por lote (`U_BatchID`) y rollback.

📘 **Guía completa para principiantes:** [docs/GUIA_DE_USO.md](docs/GUIA_DE_USO.md)

## Inicio rápido (Windows)

```powershell
git clone https://github.com/JAGA894/dsoft-carga-facturas-sap.git
cd dsoft-carga-facturas-sap
.\instalar.bat          # crea venv, instala dependencias y el archivo .env
.\iniciar_app.bat       # http://localhost:8501
```

## Arquitectura

| Archivo | Responsabilidad |
|---|---|
| `app.py` | Interfaz Streamlit (sin lógica de datos ni HTTP) |
| `procesador.py` | Lectura, validación de RFC, limpieza, fechas ISO, mapeo a UDT (pandas puro) |
| `sap_api.py` | `SAPClient`: login/logout, upsert por lote, rollback por `U_BatchID` |
| `verificar_sap.py` | Diagnóstico de conexión y de campos de la UDT |

## Pruebas

```powershell
pip install -r requirements-dev.txt
python -m pytest -v
```

## Seguridad

`.env`, `*.csv` y `*.xlsx` están excluidos en `.gitignore`. Nunca subas credenciales ni datos fiscales.
