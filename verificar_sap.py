"""
verificar_sap.py – Diagnóstico de la conexión con SAP B1
========================================================
Uso (desde la carpeta del proyecto):

    python verificar_sap.py                 → solo revisa (no modifica nada)
    python verificar_sap.py --crear-campos  → además crea U_BatchID si falta

Revisa: variables del .env, login, existencia de la UDT y que todos los
campos que la app envía existan en SAP. Siempre cierra la sesión al final.
"""

import sys

from procesador import MAPEO_COLUMNAS, MAPEO_OPCIONAL
from sap_api import SAPClient, SAPError

OK, FALLA, AVISO = "[ OK ]", "[FALLA]", "[AVISO]"


def main() -> int:
    crear = "--crear-campos" in sys.argv
    print("=" * 64)
    print(" Diagnóstico SAP B1 Service Layer")
    print("=" * 64)

    try:
        sap = SAPClient()
    except SAPError as exc:
        print(f"{FALLA} {exc}")
        return 1

    print(f"{OK} .env cargado  → URL: {sap.base_url}")
    print(f"       Base de datos: {sap.company_db} | Usuario: {sap.user} | Tabla: @{sap.tabla}")

    try:
        sap.login()
        print(f"{OK} Login correcto")

        if not sap.tabla_existe():
            print(f"{FALLA} La tabla @{sap.tabla} no existe en SAP.")
            return 1
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
            return 1

        print("-" * 64)
        print(" Todo en orden. La aplicación puede cargar datos a SAP.")
        return 0

    except SAPError as exc:
        print(f"{FALLA} {exc}")
        return 1
    finally:
        sap.logout()
        print("       Sesión SAP cerrada.")


if __name__ == "__main__":
    sys.exit(main())
