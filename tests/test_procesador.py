"""
Pruebas unitarias de procesador.py (no requieren conexión a SAP).
Ejecutar:  python -m pytest -v
"""

import io
import json
from datetime import datetime

import pandas as pd
import pytest

from procesador import (
    ErrorValidacion,
    a_payload,
    batch_id_valido,
    columnas_ignoradas,
    convertir_fecha_iso,
    convertir_numero,
    generar_batch_id,
    leer_archivo,
    limpiar,
    mapear_a_sap,
    validar_rfc,
)

ENCABEZADO = "REPORTE SAT\nEmpresa X\nPeriodo 2026\n,,,\n"
COLUMNAS = (
    "Sello,SAT,Estatus,Fecha Cancelación,Tipo,UUID,Emisión,"
    "Emisor RFC,Receptor RFC,Total,Forma Pago,Columna Basura\n"
)
FILAS = (
    "Válido,Existe,Vigente,,Ingreso,AAA-111,29/12/2025,PROV010101AAA,ESI151228L26,\"$1,234.50\",03,x\n"
    "Válido,Existe,Cancelado,05/01/2026,Ingreso,BBB-222,03/12/2025,PROV010101AAA,ESI151228L26,99.9,99,y\n"
    ",,,,,,,,,,,\n"
    "Válido,Existe,Vigente,,Egreso,,01/12/2025,PROV010101AAA,ESI151228L26,10,01,z\n"
    "Válido,Existe,Vigente,,Ingreso,AAA-111,29/12/2025,PROV010101AAA,ESI151228L26,1500,03,w\n"
)


def csv_bytes(texto: str, encoding: str = "utf-8") -> io.BytesIO:
    return io.BytesIO(texto.encode(encoding))


@pytest.fixture
def df_raw():
    return leer_archivo(csv_bytes(ENCABEZADO + COLUMNAS + FILAS), "reporte.csv")


# ── Lectura ─────────────────────────────────────────────────────────────────
def test_lectura_omite_encabezado_y_conserva_texto(df_raw):
    assert "UUID" in df_raw.columns
    assert df_raw.loc[0, "Forma Pago"] == "03"          # cero a la izquierda intacto
    assert len(df_raw) == 4                              # fila vacía eliminada


def test_lectura_cp1252():
    df = leer_archivo(csv_bytes(ENCABEZADO + COLUMNAS + FILAS, "cp1252"), "r.csv")
    assert df.loc[0, "Sello"] == "Válido"


def test_encabezados_sin_acentos_se_normalizan():
    texto = ENCABEZADO + "uuid,Sello,SAT,estatus,Emision,TOTAL,Fecha_Cancelacion,Emisor RFC\nU1,a,b,Vigente,01/02/2026,5,,X\n"
    df = leer_archivo(csv_bytes(texto), "r.csv")
    assert {"UUID", "Emisión", "Total", "Fecha Cancelación", "Estatus"} <= set(df.columns)


def test_formato_no_soportado():
    with pytest.raises(ErrorValidacion):
        leer_archivo(csv_bytes("x"), "archivo.pdf")


def test_lectura_xlsx():
    buffer = io.BytesIO()
    df = pd.DataFrame([["UUID", "Emisión", "Forma Pago"], ["U1", "29/12/2025", "03"]])
    relleno = pd.DataFrame([["Reporte"], ["x"], ["y"], ["z"]])
    with pd.ExcelWriter(buffer, engine="openpyxl") as w:
        relleno.to_excel(w, index=False, header=False, startrow=0)
        df.to_excel(w, index=False, header=False, startrow=4)
    buffer.seek(0)
    leido = leer_archivo(buffer, "reporte.xlsx")
    assert leido.loc[0, "UUID"] == "U1"
    assert leido.loc[0, "Forma Pago"] == "03"


# ── RFC ─────────────────────────────────────────────────────────────────────
def test_rfc_coincide_por_receptor(df_raw):
    assert validar_rfc(df_raw, "ESI151228L26") == "Receptor RFC"


def test_rfc_coincide_por_emisor(df_raw):
    assert validar_rfc(df_raw, "prov010101aaa") == "Emisor RFC"


def test_rfc_no_coincide(df_raw):
    with pytest.raises(ErrorValidacion, match="no pertenece"):
        validar_rfc(df_raw, "CSB1201163D6")


def test_rfc_sin_columna():
    with pytest.raises(ErrorValidacion, match="Emisor RFC"):
        validar_rfc(pd.DataFrame({"UUID": ["1"]}), "X")


# ── Limpieza (sin filtros destructivos) ─────────────────────────────────────
def test_limpiar_conserva_cancelados_y_quita_sin_uuid_y_duplicados(df_raw):
    df, info = limpiar(df_raw)
    assert set(df["Estatus"]) == {"Vigente", "Cancelado"}
    assert info == {"sin_uuid": 1, "duplicados": 1}
    assert df.loc[df["UUID"] == "AAA-111", "Total"].item() == "1500"   # se conserva el último


# ── Fechas ──────────────────────────────────────────────────────────────────
def test_fechas_iso_y_nulos():
    serie = pd.Series(["29/12/2025", "", None, "05/01/2026 10:30:00", "2025-12-29 00:00:00", "basura"])
    assert convertir_fecha_iso(serie).tolist() == [
        "2025-12-29", None, None, "2026-01-05", "2025-12-29", None,
    ]


def test_fechas_dia_primero():
    # 03/04/2026 debe ser 3 de abril, NO 4 de marzo
    assert convertir_fecha_iso(pd.Series(["03/04/2026"])).tolist() == ["2026-04-03"]


# ── Números ─────────────────────────────────────────────────────────────────
def test_numeros():
    assert convertir_numero(pd.Series(["$1,234.50", "", None, "abc", "7"])).tolist() == [
        1234.5, None, None, None, 7.0,
    ]


# ── Mapeo y payload ─────────────────────────────────────────────────────────
def test_mapeo_completo(df_raw):
    df, _ = limpiar(df_raw)
    sap = mapear_a_sap(df)

    assert list(sap.columns[:2]) == ["Code", "Name"]
    assert (sap["Code"] == sap["Name"]).all()
    assert "Columna Basura" not in sap.columns
    assert "Receptor RFC" not in sap.columns and "U_Receptor_RFC" not in sap.columns
    assert {"U_Sello", "U_SAT", "U_Estatus", "U_Emision", "U_Total",
            "U_Fecha_Cancelacion", "U_Emisor_RFC", "U_Forma_Pago", "U_Tipo"} <= set(sap.columns)
    assert "Columna Basura" in columnas_ignoradas(df)


def test_payload_json_valido_para_sap(df_raw):
    df, _ = limpiar(df_raw)
    sap = mapear_a_sap(df)
    sap["U_BatchID"] = "LOTE_20261005_0830"
    payload = a_payload(sap)

    texto = json.dumps(payload, allow_nan=False)        # falla si quedara un NaN
    cancelada = next(r for r in payload if r["Code"] == "BBB-222")
    vigente = next(r for r in payload if r["Code"] == "AAA-111")

    assert cancelada["U_Fecha_Cancelacion"] == "2026-01-05"
    assert cancelada["U_Emision"] == "2025-12-03"
    assert vigente["U_Fecha_Cancelacion"] is None
    assert '"U_Fecha_Cancelacion": null' in texto
    assert vigente["U_Total"] == 1500.0 and isinstance(vigente["U_Total"], float)
    assert vigente["U_Forma_Pago"] == "03"
    assert all(r["U_BatchID"] == "LOTE_20261005_0830" for r in payload)


def test_columnas_obligatorias_faltantes():
    with pytest.raises(ErrorValidacion, match="obligatorias"):
        mapear_a_sap(pd.DataFrame({"UUID": ["1"], "Total": ["2"]}))


# ── Batch ID ────────────────────────────────────────────────────────────────
def test_batch_id():
    assert generar_batch_id(datetime(2026, 10, 24, 15, 30)) == "LOTE_20261024_1530"
    assert batch_id_valido(generar_batch_id())
    assert not batch_id_valido("LOTE_X' or 1 eq 1")
    assert not batch_id_valido("")
