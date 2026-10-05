"""
Pruebas de la carga rápida ($batch) y de la unión de varios archivos.
No se conectan a SAP: se simulan las respuestas del Service Layer.
"""

import io
import json

import pandas as pd
import pytest

import sap_api
from procesador import mapear_a_sap, procesar_archivo, unir_archivos
from sap_api import SAPClient, _iguales


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
@pytest.fixture
def cliente(monkeypatch):
    monkeypatch.setenv("SAP_URL", "https://sap.ejemplo/b1s/v1")
    monkeypatch.setenv("SAP_COMPANYDB", "SA_PRODUCTIVA")
    monkeypatch.setenv("SAP_USER", "usuario")
    monkeypatch.setenv("SAP_PASSWORD", "clave")
    monkeypatch.setenv("SAP_UDT", "FACTURAS")
    return SAPClient()


class SAPFalso:
    """Simula _batch: ejecuta operaciones y se DETIENE en el primer error (como SAP)."""

    def __init__(self, existentes=None, fallar_codes=()):
        self.tabla = {k.upper(): dict(v) for k, v in (existentes or {}).items()}
        self.fallar = set(fallar_codes)
        self.llamadas = []

    def batch(self, ops):
        self.llamadas.append(ops)
        respuestas = []
        for metodo, ruta, cuerpo in ops:
            code = (cuerpo or {}).get("Code") or ruta.split("'")[1]
            if code in self.fallar:
                respuestas.append((400, json.dumps({"error": {"code": -5002, "message": {"value": "Dato inválido"}}})))
                break
            if metodo == "POST":
                if code.upper() in self.tabla:
                    respuestas.append((400, json.dumps({"error": {"code": -2035, "message": {"value": "Ya existe"}}})))
                    break
                self.tabla[code.upper()] = dict(cuerpo)
                respuestas.append((201, ""))
            elif metodo == "PATCH":
                self.tabla[code.upper()].update(cuerpo)
                respuestas.append((204, ""))
            elif metodo == "DELETE":
                self.tabla.pop(code.upper(), None)
                respuestas.append((204, ""))
        return respuestas


def registro(i, **extra):
    code = f"UUID-{i:04d}"
    return {"Code": code, "Name": code, "U_Estatus": "Vigente", "U_Total": 100.0 + i,
            "U_Emision": "2026-10-05", "U_BatchID": "LOTE_20261005_1200", **extra}


def preparar(cliente, monkeypatch, falso):
    monkeypatch.setattr(cliente, "registros_existentes", lambda campos: falso.tabla)
    monkeypatch.setattr(cliente, "_batch", falso.batch)


# ---------------------------------------------------------------------------
# Comparación de valores
# ---------------------------------------------------------------------------
def test_iguales_fechas_importes_y_nulos():
    assert _iguales("2025-07-14", "2025-07-14T00:00:00Z")
    assert _iguales(5013.09, 5013.0900001)
    assert not _iguales(5013.09, 5013.10)
    assert _iguales(None, 0.0)          # SAP devuelve 0.0 en importes vacíos
    assert _iguales(None, "")
    assert _iguales(" Vigente ", "Vigente")
    assert not _iguales("Válido", "VÃ¡lido")
    assert not _iguales("Cancelado", "Vigente")
    assert _iguales("PAGO 3.\nOBRA 436", "PAGO 3.\rOBRA 436")   # SAP guarda '\r' en memo


# ---------------------------------------------------------------------------
# Upsert con $batch
# ---------------------------------------------------------------------------
def test_upsert_clasifica_crear_actualizar_y_omitir(cliente, monkeypatch):
    existentes = {
        "UUID-0001": {**registro(1), "U_Emision": "2026-10-05T00:00:00Z", "U_BatchID": "LOTE_VIEJO"},  # idéntico
        "UUID-0002": {**registro(2), "U_Estatus": "Vigente", "U_PROYECTO": "001"},
    }
    falso = SAPFalso(existentes)
    preparar(cliente, monkeypatch, falso)

    payload = [registro(1), registro(2, U_Estatus="Cancelado"), registro(3)]
    res = cliente.upsert_lote(payload)

    assert (res["creados"], res["actualizados"], res["sin_cambios"], res["errores"]) == (1, 1, 1, 0)
    # PATCH solo con el campo que cambió; nunca Code ni U_BatchID
    patch = next(op for op in falso.llamadas[0] if op[0] == "PATCH")
    assert patch[2] == {"U_Estatus": "Cancelado"}
    # Campos que el archivo no trae (U_PROYECTO) se conservan; el lote original no se pisa
    assert falso.tabla["UUID-0002"]["U_PROYECTO"] == "001"
    assert falso.tabla["UUID-0001"]["U_BatchID"] == "LOTE_VIEJO"
    assert falso.tabla["UUID-0003"]["U_BatchID"] == "LOTE_20261005_1200"


def test_upsert_en_bloques_y_reanuda_tras_error(cliente, monkeypatch):
    monkeypatch.setattr(sap_api, "TAMANO_BATCH", 10)
    falso = SAPFalso(fallar_codes={"UUID-0005", "UUID-0017"})
    preparar(cliente, monkeypatch, falso)

    avances = []
    res = cliente.upsert_lote([registro(i) for i in range(25)],
                              progress_callback=lambda a, t: avances.append((a, t)))

    assert res["creados"] == 23 and res["errores"] == 2
    assert {d.split("]")[0][1:] for d in res["detalle_errores"]} == {"UUID-0005", "UUID-0017"}
    assert "Dato inválido" in res["detalle_errores"][0]
    assert len(falso.tabla) == 23                       # nada se perdió ni se repitió
    assert all(len(ops) <= 10 for ops in falso.llamadas)
    assert avances[-1] == (25, 25)


def test_post_duplicado_se_reintenta_como_patch(cliente, monkeypatch):
    falso = SAPFalso()
    preparar(cliente, monkeypatch, falso)
    # La descarga inicial no lo vio, pero ya existe al momento del POST
    monkeypatch.setattr(cliente, "registros_existentes", lambda campos: {})
    falso.tabla["UUID-0001"] = registro(1, U_Estatus="Vigente")

    res = cliente.upsert_lote([registro(1, U_Estatus="Cancelado")])
    assert (res["creados"], res["actualizados"], res["errores"]) == (0, 1, 0)
    assert falso.tabla["UUID-0001"]["U_Estatus"] == "Cancelado"


def test_eliminar_lote_en_bloques(cliente, monkeypatch):
    monkeypatch.setattr(sap_api, "TAMANO_BATCH", 7)
    falso = SAPFalso({f"UUID-{i:04d}": registro(i) for i in range(20)})
    monkeypatch.setattr(cliente, "_batch", falso.batch)
    monkeypatch.setattr(cliente, "codes_de_lote", lambda b: [f"UUID-{i:04d}" for i in range(20)])

    res = cliente.eliminar_lote("LOTE_20261005_1200")
    assert res == {"eliminados": 20, "errores": 0, "detalle_errores": []}
    assert len(falso.llamadas) == 3 and not falso.tabla


def test_batch_arma_y_lee_multipart(cliente, monkeypatch):
    capturado = {}

    class Resp:
        status_code = 202
        headers = {"Content-Type": "multipart/mixed; boundary=batchresponse_abc"}
        content = (
            "--batchresponse_abc\r\nContent-Type: application/http\r\n\r\n"
            "HTTP/1.1 201 Created\r\nContent-Type: application/json\r\n\r\n{\"Code\":\"A\"}\r\n"
            "--batchresponse_abc\r\nContent-Type: application/http\r\n\r\n"
            "HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\n\r\n"
            "{\"error\":{\"code\":-2035,\"message\":{\"value\":\"Ya existe\"}}}\r\n"
            "--batchresponse_abc--\r\n"
        ).encode("utf-8")

    def falso_request(metodo, ruta, **kwargs):
        capturado.update(metodo=metodo, ruta=ruta, **kwargs)
        return Resp()

    monkeypatch.setattr(cliente, "_request", falso_request)
    res = cliente._batch([("POST", "U_FACTURAS", {"Code": "A", "U_Sello": "Válido"}),
                          ("DELETE", "U_FACTURAS('B')", None)])

    cuerpo = capturado["data"].decode("utf-8")
    assert capturado["ruta"] == "$batch"
    assert "POST /b1s/v1/U_FACTURAS" in cuerpo and "DELETE /b1s/v1/U_FACTURAS('B')" in cuerpo
    assert capturado["headers"]["Content-Type"].startswith("multipart/mixed;boundary=")
    assert res[0][0] == 201 and res[1][0] == 400 and "-2035" in res[1][1]


# ---------------------------------------------------------------------------
# Varios archivos de la misma empresa
# ---------------------------------------------------------------------------
ENCABEZADO = "EMPRESA SA\nESI151228L26\nFacturas Recibidas\nFILTRO\n"


def csv(columnas, filas):
    texto = ENCABEZADO + ",".join(columnas) + "\n" + "\n".join(",".join(f) for f in filas) + "\n"
    return io.BytesIO(texto.encode("utf-8"))


def test_varios_archivos_cada_registro_conserva_sus_columnas():
    facturas = csv(
        ["Sello", "SAT", "Estatus", "UUID", "Emisión", "Emisor RFC", "Total", "Forma Pago", "Impto. Loc. Tras."],
        [["Válido", "Existe", "Vigente", "F-1", "01/10/2026", "PRO010101AAA", "100", "03", "5"],
         ["Válido", "Existe", "Cancelado", "F-2", "02/10/2026", "PRO010101AAA", "200", "99", ""]],
    )
    notas = csv(
        ["Sello", "SAT", "Estatus", "UUID", "Emisión", "Emisor RFC", "Total", "CFDI Relacionado"],
        [["Válido", "Existe", "Vigente", "NC-1", "03/10/2026", "PRO010101AAA", "50", "F-1"]],
    )
    p1 = procesar_archivo(facturas, "FACTURAS.csv", "ESI151228L26")
    p2 = procesar_archivo(notas, "NC.csv", "ESI151228L26")
    vista, payload, repetidos = unir_archivos([p1["df_sap"], p2["df_sap"]])

    assert len(payload) == 3 and repetidos == 0 and len(vista) == 3
    por_code = {r["Code"]: r for r in payload}
    assert por_code["F-1"]["U_Forma_Pago"] == "03"
    assert por_code["F-1"]["U_Impuesto_Local"] == 5.0          # alias 'Impto. Loc. Tras.'
    # La nota de crédito NO envía campos que su archivo no trae (no borra datos en SAP)
    assert "U_Forma_Pago" not in por_code["NC-1"]
    assert "U_Impuesto_Local" not in por_code["NC-1"]


def test_uuid_repetido_entre_archivos_se_queda_el_ultimo():
    a = mapear_a_sap(pd.DataFrame({"UUID": ["X"], "Sello": ["a"], "SAT": ["b"], "Estatus": ["Vigente"],
                                   "Emisión": ["01/10/2026"], "Total": ["1"]}))
    b = mapear_a_sap(pd.DataFrame({"UUID": ["X"], "Sello": ["a"], "SAT": ["b"], "Estatus": ["Cancelado"],
                                   "Emisión": ["01/10/2026"], "Total": ["1"]}))
    vista, payload, repetidos = unir_archivos([a, b])
    assert repetidos == 1 and len(payload) == 1 and payload[0]["U_Estatus"] == "Cancelado"


def test_archivo_de_otra_empresa_indica_cual_es():
    otro = io.BytesIO(("OTRA SA\nXXX010101XXX\nFacturas\nFILTRO\nUUID,Emisor RFC\nA,XXX010101XXX\n").encode())
    with pytest.raises(Exception, match="OTRO.csv"):
        procesar_archivo(otro, "OTRO.csv", "ESI151228L26")
