"""
verificar_sap.py – Diagnóstico de la conexión con SAP B1
========================================================
Uso (desde la carpeta del proyecto):

    python verificar_sap.py                 → solo revisa (no modifica nada)
    python verificar_sap.py --crear-campos  → además crea U_BatchID si falta

Revisa: variables del .env, login, existencia de la UDT y que todos los
campos que la app envía existan en SAP. Siempre cierra la sesión al final.
Si SAP_COMPANYDB contiene varias bases separadas por comas, revisa cada una.
"""

import os
import sys

from procesador import MAPEO_COLUMNAS, MAPEO_OPCIONAL
from sap_api import SAPClient, SAPError

OK, FALLA, AVISO = "[ OK ]", "[FALLA]", "[AVISO]"


def revisar_base(company_db: str, crear: bool) -> bool:
    """Revisa una base de datos. Devuelve True si está lista para cargar."""
    print("-" * 64)
    try:
        sap = SAPClient(company_db=company_db)
    except SAPError as exc:
        print(f"{FALLA} {exc}")
        return False

    print(f"       Base de datos: {sap.company_db} | Usuario: {sap.user} | Tabla: @{sap.tabla}")

    try:
        sap.login()
        print(f"{OK} Login correcto")

        if not sap.tabla_existe():
            print(f"{FALLA} La tabla @{sap.tabla} no existe en SAP.")
            return False
        print(f"{OK} La tabla @{sap.tabla} existe (endpoint /{sap.entidad})")

        campos = set(sap.campos_udt())
        requeridos = {
            v[2:] for v in list(MAPEO_COLUMNAS.values()) + list(MAPEO_OPCIONAL.values())
            if v.startswith("U_")
        }
        faltan = sorted(requeridos - campos)
        if faltan:
            print(f"{AVISO} Campos del mapeo que NO existen en SAP: {faltan}")
        else:
            print(f"{OK} Todos los campos del mapeo existen en SAP ({len(requeridos)})")

        if "BatchID" in campos:
            print(f"{OK} El campo U_BatchID existe (rollback disponible)")
        elif crear:
            sap.crear_campo_batch()
            print(f"{OK} Campo U_BatchID CREADO en @{sap.tabla}")
        else:
            print(f"{FALLA} Falta el campo U_BatchID. Ejecuta: python verificar_sap.py --crear-campos")
            return False
        return True

    except SAPError as exc:
        print(f"{FALLA} {exc}")
        return False
    finally:
        sap.logout()
        print("       Sesión SAP cerrada.")


def main() -> int:
    crear = "--crear-campos" in sys.argv
    print("=" * 64)
    print(" Diagnóstico SAP B1 Service Layer")
    print("=" * 64)

    bases = [b.strip() for b in os.getenv("SAP_COMPANYDB", "").split(",") if b.strip()]
    if not bases:
        print(f"{FALLA} Falta SAP_COMPANYDB en el archivo .env")
        return 1
    print(f"{OK} .env cargado  → URL: {os.getenv('SAP_URL', '').strip()}")
    print(f"       Bases a revisar: {', '.join(bases)}")

    resultados = {db: revisar_base(db, crear) for db in bases}

    print("=" * 64)
    for db, listo in resultados.items():
        print(f"{OK if listo else FALLA} {db}")
    if all(resultados.values()):
        print(" Todo en orden. La aplicación puede cargar datos a SAP.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
