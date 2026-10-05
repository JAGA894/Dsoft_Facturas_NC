"""
sap_api.py – Cliente HTTP para SAP Business One Service Layer
=============================================================
Responsabilidad única: encapsular TODA la comunicación con SAP B1.
  - Autenticación / cierre de sesión (manejo de la licencia)
  - Upsert por lote (GET → PATCH si existe, POST si no existe)
  - Rollback de un lote completo por U_BatchID
  - Verificación / creación del campo U_BatchID en la UDT

Este módulo NO conoce Streamlit; es agnóstico de la UI.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import requests
import urllib3
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Variables de entorno (archivo .env junto a este script):
#   SAP_URL        → https://<host>:<puerto>/b1s/v1   (sin slash final)
#   SAP_COMPANYDB  → Nombre exacto de la base de datos SAP
#   SAP_USER       → Usuario SAP Business One
#   SAP_PASSWORD   → Contraseña del usuario SAP
#   SAP_UDT        → Nombre de la tabla de usuario SIN '@', ej. FACTURAS
# ---------------------------------------------------------------------------
# override=True: el .env SIEMPRE tiene prioridad sobre variables de entorno de
# Windows con el mismo nombre (si no, una SAP_COMPANYDB del sistema lo pisaría
# en silencio y SAP respondería -306).
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

PATRON_BATCH_ID = re.compile(r"^LOTE_\d{8}_\d{4}$")

# Campos que NO se modifican al actualizar (PATCH) un registro existente:
#  - Code: es la llave primaria.
#  - U_BatchID: conserva el lote que CREÓ el registro. Así, revertir un lote
#    solo elimina lo que ese lote creó y nunca borra facturas que ya existían
#    antes (que solo fueron actualizadas).
CAMPOS_NO_ACTUALIZABLES = ("Code", "U_BatchID")


class SAPError(Exception):
    """Error de comunicación o de negocio devuelto por SAP Service Layer."""


def _mensaje_error(resp: requests.Response) -> str:
    """Extrae el mensaje legible del JSON de error de Service Layer."""
    try:
        err = resp.json().get("error", {})
        msg = err.get("message", {})
        texto = msg.get("value") if isinstance(msg, dict) else str(msg)
        return f"HTTP {resp.status_code} (código SAP {err.get('code')}): {texto}"
    except ValueError:
        return f"HTTP {resp.status_code}: {resp.text[:300]}"


class SAPClient:
    """
    Cliente de sesión para SAP B1 Service Layer.

    - requests.Session conserva las cookies B1SESSION/ROUTEID entre llamadas.
    - verify=False: Service Layer suele usar certificados autofirmados.
    - Uso correcto: login() → operaciones → logout() (en un finally), o bien
      como context manager:  with SAPClient() as sap: ...
    """

    def __init__(self, timeout: int = 60):
        self.base_url = os.getenv("SAP_URL", "").strip().rstrip("/")
        self.company_db = os.getenv("SAP_COMPANYDB", "").strip()
        self.user = os.getenv("SAP_USER", "").strip()
        self.password = os.getenv("SAP_PASSWORD", "")
        udt = os.getenv("SAP_UDT", "").strip().lstrip("@")
        self.timeout = timeout
        self._autenticado = False

        faltantes = [
            nombre for nombre, valor in {
                "SAP_URL": self.base_url, "SAP_COMPANYDB": self.company_db,
                "SAP_USER": self.user, "SAP_PASSWORD": self.password, "SAP_UDT": udt,
            }.items() if not valor
        ]
        if faltantes:
            raise SAPError(f"Faltan variables en el archivo .env: {', '.join(faltantes)}")

        # Service Layer expone las tablas de usuario como 'U_<TABLA>'.
        # (GET /FACTURAS → "Unrecognized resource path"; GET /U_FACTURAS → OK)
        self.tabla = udt[2:] if udt.upper().startswith("U_") else udt   # FACTURAS
        self.entidad = f"U_{self.tabla}"                                 # U_FACTURAS

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers.update({
            "Content-Type": "application/json",
            "Prefer": "return=minimal",   # POST/PATCH sin devolver el objeto
        })

    # -----------------------------------------------------------------------
    # Infraestructura HTTP
    # -----------------------------------------------------------------------
    def __enter__(self):
        self.login()
        return self

    def __exit__(self, *exc):
        self.logout()
        return False

    def _request(self, metodo: str, ruta: str, reintentar: bool = True, **kwargs):
        """
        Petición HTTP con timeout. Si la sesión SAP expiró (HTTP 401) durante
        un lote largo, vuelve a autenticarse una vez y reintenta.
        """
        kwargs.setdefault("timeout", self.timeout)
        url = ruta if ruta.startswith("http") else f"{self.base_url}/{ruta.lstrip('/')}"
        try:
            resp = self.session.request(metodo, url, **kwargs)
            if resp.status_code == 401 and reintentar and self._autenticado:
                self.login()
                resp = self.session.request(metodo, url, **kwargs)
            return resp
        except requests.RequestException as exc:
            raise SAPError(f"No se pudo conectar con SAP ({type(exc).__name__}): {exc}") from exc

    @staticmethod
    def _llave(code: str) -> str:
        """Escapa comillas simples para OData: O'Brien → O''Brien."""
        return str(code).replace("'", "''")

    # -----------------------------------------------------------------------
    # Sesión
    # -----------------------------------------------------------------------
    def login(self) -> None:
        """Abre sesión en SAP. Lanza SAPError si las credenciales fallan."""
        resp = self._request(
            "POST", "Login", reintentar=False,
            json={"CompanyDB": self.company_db, "UserName": self.user, "Password": self.password},
        )
        if resp.status_code != 200:
            detalle = _mensaje_error(resp)
            # Códigos verificados contra este Service Layer:
            #   -306 → CompanyDB inexistente o mal escrita (distingue mayúsculas)
            #   -304 → usuario o contraseña incorrectos
            if "-306" in detalle:
                pista = (f" | Causa: SAP no reconoce la base de datos SAP_COMPANYDB='{self.company_db}'. "
                         "Revisa en el .env que esté escrita EXACTAMENTE igual (mayúsculas incluidas).")
            elif "-304" in detalle:
                pista = f" | Causa: usuario o contraseña incorrectos para SAP_USER='{self.user}'."
            else:
                pista = ""
            raise SAPError(f"Error al autenticar en SAP Service Layer. {detalle}{pista}")
        self._autenticado = True

    def logout(self) -> None:
        """
        Cierra la sesión y libera la licencia SAP. Se llama SIEMPRE en un
        finally. Los errores se silencian para no ocultar el error original.
        """
        if not self._autenticado:
            return
        try:
            self.session.post(f"{self.base_url}/Logout", timeout=15)
        except Exception:
            pass
        finally:
            self._autenticado = False

    # -----------------------------------------------------------------------
    # Metadatos de la UDT
    # -----------------------------------------------------------------------
    def campos_udt(self) -> list[str]:
        """Nombres de los campos de usuario de la UDT (sin el prefijo U_)."""
        resp = self._request(
            "GET", "UserFieldsMD",
            params={"$filter": f"TableName eq '@{self.tabla}'", "$select": "Name"},
            headers={"Prefer": "odata.maxpagesize=500"},
        )
        if resp.status_code != 200:
            raise SAPError(f"No se pudieron leer los campos de @{self.tabla}. {_mensaje_error(resp)}")
        return [c["Name"] for c in resp.json().get("value", [])]

    def tabla_existe(self) -> bool:
        resp = self._request("GET", f"UserTablesMD('{self.tabla}')")
        return resp.status_code == 200

    def crear_campo_batch(self) -> None:
        """Crea el campo U_BatchID (alfanumérico, 30) en la UDT."""
        resp = self._request("POST", "UserFieldsMD", json={
            "Name": "BatchID",
            "TableName": f"@{self.tabla}",
            "Description": "Lote de carga (rollback)",
            "Type": "db_Alpha",
            "Size": 30,
            "EditSize": 30,
        })
        if resp.status_code not in (200, 201, 204):
            raise SAPError(f"No se pudo crear el campo U_BatchID. {_mensaje_error(resp)}")

    # -----------------------------------------------------------------------
    # Upsert por lote
    # -----------------------------------------------------------------------
    def upsert_lote(self, payload_list: list[dict], progress_callback=None) -> dict:
        """
        Por cada registro:
          GET /U_<TABLA>('<Code>') → 200 → PATCH (actualizar, sin Code ni U_BatchID)
                                   → 404 → POST  (crear, con U_BatchID)
        progress_callback(actual, total) se invoca tras cada registro.

        Returns: {'creados', 'actualizados', 'errores', 'detalle_errores'}
        """
        total = len(payload_list)
        res = {"creados": 0, "actualizados": 0, "errores": 0, "detalle_errores": []}

        for idx, registro in enumerate(payload_list, start=1):
            code = str(registro.get("Code") or "").strip()
            try:
                if not code:
                    raise SAPError("Registro sin Code (UUID vacío).")
                ruta = f"{self.entidad}('{self._llave(code)}')"

                existe = self._request("GET", ruta, params={"$select": "Code"})
                if existe.status_code == 200:
                    cuerpo = {k: v for k, v in registro.items() if k not in CAMPOS_NO_ACTUALIZABLES}
                    resp = self._request("PATCH", ruta, json=cuerpo)
                    if resp.status_code not in (200, 204):
                        raise SAPError(f"Actualización fallida. {_mensaje_error(resp)}")
                    res["actualizados"] += 1
                elif existe.status_code == 404:
                    resp = self._request("POST", self.entidad, json=registro)
                    if resp.status_code not in (200, 201, 204):
                        raise SAPError(f"Creación fallida. {_mensaje_error(resp)}")
                    res["creados"] += 1
                else:
                    raise SAPError(f"Consulta fallida. {_mensaje_error(existe)}")

            except SAPError as exc:
                res["errores"] += 1
                res["detalle_errores"].append(f"[{code or f'FILA_{idx}'}] {exc}")
            finally:
                if progress_callback:
                    progress_callback(idx, total)

        return res

    # -----------------------------------------------------------------------
    # Rollback por lote
    # -----------------------------------------------------------------------
    def _validar_batch(self, batch_id: str) -> str:
        batch_id = str(batch_id or "").strip()
        if not PATRON_BATCH_ID.match(batch_id):
            raise SAPError(f"Batch ID inválido: '{batch_id}'. Formato esperado: LOTE_YYYYMMDD_HHMM")
        return batch_id

    def codes_de_lote(self, batch_id: str) -> list[str]:
        """Todos los Code cuyo U_BatchID coincide (sigue la paginación OData)."""
        batch_id = self._validar_batch(batch_id)
        codes: list[str] = []
        siguiente = self.entidad
        params = {"$filter": f"U_BatchID eq '{batch_id}'", "$select": "Code"}
        while siguiente:
            resp = self._request("GET", siguiente, params=params,
                                 headers={"Prefer": "odata.maxpagesize=500"})
            if resp.status_code != 200:
                raise SAPError(f"No se pudo consultar el lote. {_mensaje_error(resp)}")
            datos = resp.json()
            codes.extend(r["Code"] for r in datos.get("value", []))
            siguiente = datos.get("odata.nextLink") or datos.get("@odata.nextLink")
            params = None   # el nextLink ya incluye el filtro
        return codes

    def eliminar_lote(self, batch_id: str, progress_callback=None) -> dict:
        """
        Rollback: elimina (DELETE) todos los registros creados por el lote.
        Returns: {'eliminados', 'errores', 'detalle_errores'}
        """
        codes = self.codes_de_lote(batch_id)
        total = len(codes)
        res = {"eliminados": 0, "errores": 0, "detalle_errores": []}

        for idx, code in enumerate(codes, start=1):
            try:
                resp = self._request("DELETE", f"{self.entidad}('{self._llave(code)}')")
                if resp.status_code in (200, 204):
                    res["eliminados"] += 1
                else:
                    raise SAPError(_mensaje_error(resp))
            except SAPError as exc:
                res["errores"] += 1
                res["detalle_errores"].append(f"[{code}] {exc}")
            finally:
                if progress_callback:
                    progress_callback(idx, total)

        return res
