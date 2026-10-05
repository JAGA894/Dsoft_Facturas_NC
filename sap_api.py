"""
sap_api.py – Cliente HTTP para SAP Business One Service Layer
=============================================================
Responsabilidad única: encapsular TODA la comunicación con SAP B1.
  - Autenticación / cierre de sesión (manejo de la licencia)
  - Upsert por lote: descarga única de existentes + POST/PATCH en $batch
  - Rollback de un lote completo por U_BatchID
  - Verificación / creación del campo U_BatchID en la UDT

Este módulo NO conoce Streamlit; es agnóstico de la UI.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlparse

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

# Operaciones por petición $batch. Medido en este Service Layer:
# 100 POST ≈ 5 s (vs ~0.5 s por registro con peticiones individuales).
TAMANO_BATCH = 100


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


def _error_de_texto(status: int, texto: str) -> str:
    """Igual que _mensaje_error, pero para una respuesta dentro de un $batch."""
    try:
        err = json.loads(texto).get("error", {})
        msg = err.get("message", {})
        valor = msg.get("value") if isinstance(msg, dict) else str(msg)
        return f"HTTP {status} (código SAP {err.get('code')}): {valor}"
    except (ValueError, AttributeError):
        return f"HTTP {status}: {texto[:300]}" if status else texto[:300]


_FECHA_SAP = re.compile(r"^\d{4}-\d{2}-\d{2}T")   # SAP devuelve '2025-07-14T00:00:00Z'


def _normalizar(valor):
    if valor is None:
        return None
    if isinstance(valor, str):
        # SAP guarda los saltos de línea de los campos memo como '\r'
        valor = valor.replace("\r\n", "\n").replace("\r", "\n").strip()
        if _FECHA_SAP.match(valor):
            valor = valor[:10]
        return valor or None
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, (int, float)):
        return float(valor)
    return valor


def _iguales(nuevo, actual) -> bool:
    """
    ¿El valor del archivo es igual al que ya tiene SAP?
    - Fechas: compara solo YYYY-MM-DD.
    - Importes: tolerancia de medio centavo; null y 0 se consideran iguales
      (SAP devuelve 0.0 en campos numéricos vacíos).
    - Texto: sin espacios laterales; '' y null son iguales.
    """
    a, b = _normalizar(nuevo), _normalizar(actual)
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a or 0.0) - float(b or 0.0)) < 0.005
        except (TypeError, ValueError):
            return False
    return a == b


class SAPClient:
    """
    Cliente de sesión para SAP B1 Service Layer.

    - requests.Session conserva las cookies B1SESSION/ROUTEID entre llamadas.
    - verify=False: Service Layer suele usar certificados autofirmados.
    - Uso correcto: login() → operaciones → logout() (en un finally), o bien
      como context manager:  with SAPClient() as sap: ...
    """

    def __init__(self, timeout: int = 60, company_db: str | None = None):
        self.base_url = os.getenv("SAP_URL", "").strip().rstrip("/")
        # SAP_COMPANYDB puede ser UNA base o VARIAS separadas por comas
        # (una por empresa). Si es lista, funciona como lista de bases permitidas
        # y la app indica cuál usar según la empresa seleccionada.
        bases_env = [b.strip() for b in os.getenv("SAP_COMPANYDB", "").split(",") if b.strip()]
        if company_db:
            company_db = company_db.strip()
            if bases_env and company_db not in bases_env:
                raise SAPError(
                    f"La base de datos '{company_db}' no está en SAP_COMPANYDB del .env "
                    f"({', '.join(bases_env)})."
                )
            self.company_db = company_db
        elif len(bases_env) > 1:
            raise SAPError(
                "SAP_COMPANYDB del .env contiene varias bases de datos; "
                "se debe indicar cuál usar según la empresa seleccionada."
            )
        else:
            self.company_db = bases_env[0] if bases_env else ""
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
    # $batch: varias operaciones en UNA petición HTTP
    # -----------------------------------------------------------------------
    def _batch(self, ops: list[tuple[str, str, dict | None]]) -> list[tuple[int, str]]:
        """
        Envía [(método, ruta, cuerpo)] en una sola petición OData $batch.
        Devuelve [(status, cuerpo_respuesta)] en el mismo orden.

        IMPORTANTE (verificado en este Service Layer): si una operación falla,
        SAP DETIENE el lote y no ejecuta las siguientes; por eso la lista
        devuelta puede ser más corta que `ops` (ver _ejecutar).
        """
        frontera = f"batch_{uuid.uuid4().hex}"
        ruta_base = urlparse(self.base_url).path.rstrip("/")     # /b1s/v1
        partes = []
        for metodo, ruta, cuerpo in ops:
            lineas = [f"--{frontera}", "Content-Type: application/http",
                      "Content-Transfer-Encoding: binary", "", f"{metodo} {ruta_base}/{ruta}"]
            if cuerpo is not None:
                lineas += ["Content-Type: application/json", "", json.dumps(cuerpo)]
            else:
                lineas += [""]
            partes.append("\r\n".join(lineas))
        datos = "\r\n".join(partes) + f"\r\n--{frontera}--\r\n"

        resp = self._request(
            "POST", "$batch", data=datos.encode("utf-8"),
            headers={"Content-Type": f"multipart/mixed;boundary={frontera}"},
        )
        if resp.status_code not in (200, 202):
            raise SAPError(f"Petición $batch rechazada. {_mensaje_error(resp)}")

        m = re.search(r'boundary="?([^";]+)"?', resp.headers.get("Content-Type", ""))
        if not m:
            raise SAPError("Respuesta $batch inesperada de SAP (sin boundary).")
        texto = resp.content.decode("utf-8", errors="replace")
        resultados = []
        for parte in texto.split(f"--{m.group(1)}"):
            estado = re.search(r"HTTP/1\.\d\s+(\d{3})", parte)
            if not estado:
                continue
            resto = re.split(r"\r?\n\r?\n", parte[estado.end():], maxsplit=1)
            resultados.append((int(estado.group(1)), resto[1].strip() if len(resto) > 1 else ""))
        return resultados

    def _ejecutar(self, ops: list[dict], avance=None) -> list[tuple[dict, int, str]]:
        """
        Ejecuta operaciones {'metodo','ruta','cuerpo',...} en bloques de
        TAMANO_BATCH. Como SAP detiene el bloque en el primer error, se reanuda
        justo después de la operación que falló, sin perder ni repetir ninguna.
        avance(n_procesadas) se invoca tras cada bloque.
        """
        resultados: list[tuple[dict, int, str]] = []
        i = 0
        while i < len(ops):
            bloque = ops[i:i + TAMANO_BATCH]
            try:
                respuestas = self._batch([(o["metodo"], o["ruta"], o["cuerpo"]) for o in bloque])
            except SAPError as exc:
                # Falla del bloque completo (red, timeout…): se reporta y se sigue.
                # Volver a subir el archivo corrige lo pendiente (el upsert es idempotente).
                respuestas = [(0, str(exc))] * len(bloque)
            if not respuestas:
                respuestas = [(0, "SAP no devolvió respuesta para esta operación.")]
            respuestas = respuestas[:len(bloque)]
            resultados.extend((op, status, cuerpo) for op, (status, cuerpo) in zip(bloque, respuestas))
            i += len(respuestas)
            if avance:
                avance(len(resultados))
        return resultados

    # -----------------------------------------------------------------------
    # Upsert por lote
    # -----------------------------------------------------------------------
    def registros_existentes(self, campos) -> dict[str, dict]:
        """
        Descarga en páginas de 1000 TODOS los registros de la UDT (solo los
        campos indicados). Reemplaza miles de GET individuales por unas
        cuantas peticiones (~20 s para 33,000 registros).
        Llave: Code en MAYÚSCULAS.
        """
        select = ",".join(sorted({"Code", *campos}))
        existentes: dict[str, dict] = {}
        siguiente, params = self.entidad, {"$select": select}
        while siguiente:
            resp = self._request("GET", siguiente, params=params,
                                 headers={"Prefer": "odata.maxpagesize=1000"})
            if resp.status_code != 200:
                raise SAPError(f"No se pudieron leer los registros existentes. {_mensaje_error(resp)}")
            datos = resp.json()
            for r in datos.get("value", []):
                existentes[str(r["Code"]).upper()] = r
            siguiente = datos.get("odata.nextLink") or datos.get("@odata.nextLink")
            params = None   # el nextLink ya incluye $select
        return existentes

    def upsert_lote(self, payload_list: list[dict], progress_callback=None,
                    mensaje_callback=None) -> dict:
        """
        Carga optimizada (antes: GET + POST/PATCH individuales por registro):

          1. Descarga una sola vez los registros que ya existen en SAP.
          2. Clasifica cada registro:
               - no existe            → POST  (crear, con U_BatchID)
               - existe con cambios   → PATCH solo con los campos distintos
               - existe idéntico      → se omite (no se envía nada)
          3. Envía los POST/PATCH en bloques $batch de TAMANO_BATCH.
          4. Si un POST falla porque el registro ya existía (-2035), se
             reintenta como PATCH.

        progress_callback(actual, total) · mensaje_callback(texto)
        Returns: {'creados', 'actualizados', 'sin_cambios', 'errores', 'detalle_errores'}
        """
        total = len(payload_list)
        res = {"creados": 0, "actualizados": 0, "sin_cambios": 0, "errores": 0, "detalle_errores": []}

        def avisar(texto: str) -> None:
            if mensaje_callback:
                mensaje_callback(texto)

        campos = {k for r in payload_list for k in r if k not in CAMPOS_NO_ACTUALIZABLES}
        avisar("🔎 Consultando los registros que ya existen en SAP…")
        existentes = self.registros_existentes(campos)

        ops: list[dict] = []
        for idx, registro in enumerate(payload_list, start=1):
            code = str(registro.get("Code") or "").strip()
            if not code:
                res["errores"] += 1
                res["detalle_errores"].append(f"[FILA_{idx}] Registro sin Code (UUID vacío).")
                continue
            actual = existentes.get(code.upper())
            if actual is None:
                ops.append({"tipo": "crear", "code": code, "metodo": "POST",
                            "ruta": self.entidad, "cuerpo": registro})
                continue
            cambios = {k: v for k, v in registro.items()
                       if k not in CAMPOS_NO_ACTUALIZABLES and not _iguales(v, actual.get(k))}
            if cambios:
                ops.append({"tipo": "actualizar", "code": code, "metodo": "PATCH",
                            "ruta": f"{self.entidad}('{self._llave(actual['Code'])}')",
                            "cuerpo": cambios})
            else:
                res["sin_cambios"] += 1

        ya_resueltos = res["sin_cambios"] + res["errores"]
        if progress_callback:
            progress_callback(ya_resueltos, total)
        n_crear = sum(op["tipo"] == "crear" for op in ops)
        avisar(f"🚀 Enviando a SAP: {n_crear} nuevo(s), {len(ops) - n_crear} con cambios, "
               f"{res['sin_cambios']} sin cambios (se omiten)…")

        avance = (lambda n: progress_callback(min(ya_resueltos + n, total), total)) if progress_callback else None
        reintentos: list[dict] = []
        for op, status, cuerpo in self._ejecutar(ops, avance):
            if status in (200, 201, 204):
                res["creados" if op["tipo"] == "crear" else "actualizados"] += 1
            elif op["tipo"] == "crear" and "-2035" in cuerpo:
                # Ya existía (p. ej. lo creó un bloque que expiró por timeout).
                reintentos.append({
                    "tipo": "actualizar", "code": op["code"], "metodo": "PATCH",
                    "ruta": f"{self.entidad}('{self._llave(op['code'])}')",
                    "cuerpo": {k: v for k, v in op["cuerpo"].items() if k not in CAMPOS_NO_ACTUALIZABLES},
                })
            else:
                accion = "Creación" if op["tipo"] == "crear" else "Actualización"
                res["errores"] += 1
                res["detalle_errores"].append(f"[{op['code']}] {accion} fallida. {_error_de_texto(status, cuerpo)}")

        for op, status, cuerpo in self._ejecutar(reintentos):
            if status in (200, 204):
                res["actualizados"] += 1
            else:
                res["errores"] += 1
                res["detalle_errores"].append(f"[{op['code']}] Actualización fallida. {_error_de_texto(status, cuerpo)}")

        if progress_callback:
            progress_callback(total, total)
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
        Rollback: elimina (DELETE) todos los registros creados por el lote,
        enviados en bloques $batch.
        Returns: {'eliminados', 'errores', 'detalle_errores'}
        """
        codes = self.codes_de_lote(batch_id)
        total = len(codes)
        res = {"eliminados": 0, "errores": 0, "detalle_errores": []}
        ops = [
            {"tipo": "eliminar", "code": code, "metodo": "DELETE",
             "ruta": f"{self.entidad}('{self._llave(code)}')", "cuerpo": None}
            for code in codes
        ]
        avance = (lambda n: progress_callback(n, total)) if progress_callback else None

        for op, status, cuerpo in self._ejecutar(ops, avance):
            if status in (200, 204):
                res["eliminados"] += 1
            else:
                res["errores"] += 1
                res["detalle_errores"].append(f"[{op['code']}] {_error_de_texto(status, cuerpo)}")
        return res
