"""
procesador.py – Transformación de datos (pandas) hacia la UDT de SAP B1
=======================================================================
Responsabilidad única: leer el archivo exportado del SAT, validarlo y
convertirlo al esquema exacto de la tabla de usuario (UDT) en SAP.

Este módulo NO conoce Streamlit ni SAP: son funciones puras de pandas,
lo que permite probarlas con pytest (ver carpeta tests/).
Los errores de negocio se lanzan como `ErrorValidacion` y la UI decide
cómo mostrarlos.
"""

from __future__ import annotations

import io
import math
import re
import unicodedata
from datetime import datetime

import pandas as pd

# Número de filas de encabezado administrativo que trae el archivo del SAT
# antes de la fila con los nombres de columna.
FILAS_ENCABEZADO = 4


class ErrorValidacion(Exception):
    """Error de negocio: el archivo no es apto para cargarse a SAP."""


# ---------------------------------------------------------------------------
# Mapeo de columnas Excel → SAP UDT (@FACTURAS)
#
#   clave = nombre de columna en el archivo del SAT
#   valor = nombre del campo en la UDT de SAP B1 (Service Layer)
#
# MAPEO_COLUMNAS → OBLIGATORIAS: si falta alguna, el archivo se rechaza.
# MAPEO_OPCIONAL → se envían solo si vienen en el archivo. Corresponden a
#                  campos que YA existen en la UDT @FACTURAS de SAP.
#
# Regla 'Code'/'Name': toda UDT exige ambos campos. Los dos reciben el UUID.
# Cualquier columna que no aparezca aquí se ignora y se elimina.
# ---------------------------------------------------------------------------
MAPEO_COLUMNAS = {
    "UUID":    "Code",        # PK de la UDT (Name recibe el mismo valor)
    "Sello":   "U_Sello",
    "SAT":     "U_SAT",
    "Estatus": "U_Estatus",   # Vigente / Cancelado (NO se filtra)
    "Emisión": "U_Emision",   # Fecha → ISO YYYY-MM-DD
    "Total":   "U_Total",
}

MAPEO_OPCIONAL = {
    "Fecha Cancelación":     "U_Fecha_Cancelacion",   # Fecha → ISO / null
    "Ver":                   "U_Ver",
    "Tipo":                  "U_Tipo",
    "Serie":                 "U_Serie",
    "Folio":                 "U_Folio",
    "Uso CFDI":              "U_Uso_CFDI",
    "Emisor RFC":            "U_Emisor_RFC",
    "Emisor Nombre":         "U_Emisor_Nombre",
    "Conceptos Descripcion": "U_Conceptos_Descripcion",
    "Subtotal":              "U_Subtotal",
    "Descuento":             "U_Descuento",
    "IVA":                   "U_IVA",
    "Impuesto Local":        "U_Impuesto_Local",
    "IVA Retenido":          "U_IVA_Retenido",
    "ISR Retenido":          "U_ISR_Retenido",
    "Impuesto Local R":      "U_Impuesto_Local_R",
    "Tipo Cambio":           "U_Tipo_Cambio",
    "Forma Pago":            "U_Forma_Pago",
    "Metodo Pago":           "U_Metodo_Pago",
}

# Columnas con fecha DD/MM/YYYY que SAP exige en ISO YYYY-MM-DD.
COLUMNAS_FECHA = ["Emisión", "Fecha Cancelación"]

# Columnas de importe (campos db_Float en SAP).
COLUMNAS_NUMERICAS = [
    "Total", "Subtotal", "Descuento", "IVA", "Impuesto Local",
    "IVA Retenido", "ISR Retenido", "Impuesto Local R", "Tipo Cambio",
]

# Columnas usadas para validar la empresa (no todas se envían a SAP).
COLUMNAS_RFC = ["Emisor RFC", "Receptor RFC"]

# Nombres alternativos que algunas versiones del reporte usan.
ALIAS_COLUMNAS = {
    "fecha_cancelacion":       "Fecha Cancelación",
    "fecha_de_cancelacion":    "Fecha Cancelación",
    "emision":                 "Emisión",
    "fecha_emision":           "Emisión",
    "fecha_de_emision":        "Emisión",
    "version":                 "Ver",
    "impuesto_local_retenido": "Impuesto Local R",
    "rfc_emisor":              "Emisor RFC",
    "rfc_receptor":            "Receptor RFC",
    "forma_de_pago":           "Forma Pago",
    "metodo_de_pago":          "Metodo Pago",
    # El reporte del SAT abrevia los impuestos locales:
    "impto_loc_tras":          "Impuesto Local",     # 'Impto. Loc. Tras.'
    "impuesto_local_trasladado": "Impuesto Local",
    "impto_loc_ret":           "Impuesto Local R",   # 'Impto. Loc. Ret.'
}

PATRON_BATCH_ID = re.compile(r"^LOTE_\d{8}_\d{4}$")


# ===========================================================================
# Utilidades
# ===========================================================================
def normalizar_nombre(texto: str) -> str:
    """'Fecha Cancelación ' → 'fecha_cancelacion' (sin acentos ni espacios)."""
    texto = unicodedata.normalize("NFKD", str(texto))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = re.sub(r"[^0-9a-zA-Z]+", "_", texto).strip("_").lower()
    return texto


def _columnas_conocidas() -> dict[str, str]:
    """Diccionario {nombre_normalizado: nombre_canónico}."""
    canonicos = list(MAPEO_COLUMNAS) + list(MAPEO_OPCIONAL) + COLUMNAS_RFC
    conocidas = {normalizar_nombre(c): c for c in canonicos}
    conocidas.update(ALIAS_COLUMNAS)
    return conocidas


def generar_batch_id(ahora: datetime | None = None) -> str:
    """
    Genera el identificador de lote, p. ej. 'LOTE_20261024_1530'.
    Se envía a SAP en U_BatchID para poder revertir (DELETE masivo) el lote.
    """
    return (ahora or datetime.now()).strftime("LOTE_%Y%m%d_%H%M")


def batch_id_valido(batch_id: str) -> bool:
    return bool(PATRON_BATCH_ID.match(str(batch_id or "").strip()))


# ===========================================================================
# 1. Lectura del archivo
# ===========================================================================
def leer_archivo(archivo, nombre_archivo: str) -> pd.DataFrame:
    """
    Lee un CSV o XLSX omitiendo las primeras FILAS_ENCABEZADO filas.

    - Todo se lee como TEXTO (dtype=str) para no perder ceros a la izquierda
      (ej. Forma Pago '03') ni alterar UUIDs. Los tipos se convierten después.
    - CSV: intenta UTF-8 y, si falla, Windows-1252 (Excel en español).
    - Los encabezados se normalizan a los nombres canónicos del mapeo
      (tolera acentos, mayúsculas, guiones bajos y espacios extra).
    """
    if hasattr(archivo, "getvalue"):          # UploadedFile de Streamlit / BytesIO
        datos = archivo.getvalue()
    elif hasattr(archivo, "read"):
        datos = archivo.read()
    else:
        with open(archivo, "rb") as f:
            datos = f.read()
    nombre = nombre_archivo.lower()
    encabezado_raw = ""

    if nombre.endswith(".xlsx"):
        df = pd.read_excel(io.BytesIO(datos), skiprows=FILAS_ENCABEZADO, dtype=str)
        try:
            df_head = pd.read_excel(io.BytesIO(datos), nrows=FILAS_ENCABEZADO, header=None, dtype=str)
            encabezado_raw = df_head.to_string().upper()
        except Exception:
            pass
    elif nombre.endswith(".csv"):
        df = None
        for codificacion in ("utf-8-sig", "cp1252"):
            try:
                df = pd.read_csv(
                    io.BytesIO(datos),
                    skiprows=FILAS_ENCABEZADO,
                    dtype=str,
                    encoding=codificacion,
                    keep_default_na=True,
                )
                try:
                    encabezado_raw = "\n".join(datos.decode(codificacion).splitlines()[:FILAS_ENCABEZADO]).upper()
                except Exception:
                    pass
                break
            except UnicodeDecodeError:
                continue
        if df is None:
            raise ErrorValidacion("No se pudo leer el CSV: codificación desconocida.")
    else:
        raise ErrorValidacion("Formato no soportado. Sube un archivo .csv o .xlsx.")

    # Normalizar encabezados → nombres canónicos
    conocidas = _columnas_conocidas()
    nuevos = []
    for col in df.columns:
        limpio = str(col).strip()
        nuevos.append(conocidas.get(normalizar_nombre(limpio), limpio))
    df.columns = nuevos

    # Quitar filas totalmente vacías y espacios laterales en cada celda
    df = df.dropna(how="all")
    for col in df.columns:
        df[col] = df[col].astype("string").str.strip()
        df[col] = df[col].where(df[col] != "", pd.NA)

    df = df.reset_index(drop=True)
    df.attrs["encabezado_raw"] = encabezado_raw
    return df


# ===========================================================================
# 2. Prevención de error humano (RFC)
# ===========================================================================
def _primer_valor(df: pd.DataFrame, columna: str) -> str | None:
    if columna not in df.columns:
        return None
    valores = df[columna].dropna()
    return str(valores.iloc[0]).strip().upper() if len(valores) else None


def validar_rfc(df: pd.DataFrame, rfc_esperado: str) -> str:
    """
    Compara el RFC del archivo con el de la empresa seleccionada.

    Regla principal: columna 'Emisor RFC'. Como la UDT también almacena
    facturas RECIBIDAS (donde el emisor es el proveedor y la empresa es el
    receptor), se acepta además 'Receptor RFC' si el archivo la trae.

    Returns: nombre de la columna que coincidió.
    Raises:  ErrorValidacion si no hay coincidencia.
    """
    if not any(c in df.columns for c in COLUMNAS_RFC):
        raise ErrorValidacion(
            "El archivo no contiene la columna 'Emisor RFC'. "
            "Verifica que sea el reporte correcto del SAT."
        )

    esperado = str(rfc_esperado).strip().upper()
    
    if esperado and esperado in df.attrs.get("encabezado_raw", ""):
        return "Encabezado del reporte"

    emisor = _primer_valor(df, "Emisor RFC")
    receptor = _primer_valor(df, "Receptor RFC")

    if emisor == esperado:
        return "Emisor RFC"
    if receptor == esperado:
        return "Receptor RFC"

    encontrado = f"Emisor RFC = `{emisor}`"
    if "Receptor RFC" in df.columns:
        encontrado += f", Receptor RFC = `{receptor}`"
    raise ErrorValidacion(
        f"El archivo no pertenece a la empresa seleccionada (RFC `{esperado}`). "
        f"En el archivo se encontró: {encontrado}."
    )


# ===========================================================================
# 3. Limpieza mínima (sin filtros destructivos)
# ===========================================================================
def limpiar(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """
    NO filtra por Estatus: se procesa todo el historial (Vigente, Cancelado…).
    Solo descarta:
      - filas sin UUID (sin PK no se puede crear el registro en SAP)
      - UUIDs repetidos dentro del mismo archivo (se conserva el último)
    """
    if "UUID" not in df.columns:
        raise ErrorValidacion("El archivo no contiene la columna obligatoria 'UUID'.")

    total = len(df)
    df = df[df["UUID"].notna()]
    sin_uuid = total - len(df)

    antes = len(df)
    df = df.drop_duplicates(subset="UUID", keep="last")
    duplicados = antes - len(df)

    return df.reset_index(drop=True), {"sin_uuid": sin_uuid, "duplicados": duplicados}


# ===========================================================================
# 4. Conversión de tipos
# ===========================================================================
def convertir_fecha_iso(serie: pd.Series) -> pd.Series:
    """
    'DD/MM/YYYY' → 'YYYY-MM-DD' (formato ISO que exige SAP Service Layer).

    1. Intenta el formato exacto DD/MM/YYYY (texto del reporte del SAT).
    2. Fechas ya en ISO 'YYYY-MM-DD[ HH:MM:SS]': así llegan las celdas que
       Excel guarda como FECHA real. Se leen tal cual, SIN 'día primero'
       (antes se interpretaban con dayfirst y se intercambiaban día y mes:
       '2026-09-11' → '2026-11-09').
    3. Cualquier otro formato se infiere con dayfirst=True.
    4. Vacíos o inválidos (NaT) → None, que en JSON se convierte en null.
       (strftime convertiría NaT en el texto 'NaT', que SAP rechaza).
    """
    texto = serie.astype("string").str.strip()
    fechas = pd.to_datetime(texto, format="%d/%m/%Y", errors="coerce")

    es_iso = texto.str.match(r"^\d{4}-\d{2}-\d{2}", na=False) & fechas.isna()
    if es_iso.any():
        iso_parseadas = pd.to_datetime(texto[es_iso].str[:10], format="%Y-%m-%d", errors="coerce")
        fechas = fechas.where(~es_iso, iso_parseadas)

    pendientes = fechas.isna() & texto.notna() & ~es_iso
    if pendientes.any():
        inferidas = pd.to_datetime(
            texto[pendientes], dayfirst=True, format="mixed", errors="coerce"
        )
        fechas = fechas.where(~pendientes, inferidas)

    iso = fechas.dt.strftime("%Y-%m-%d").astype(object)
    return iso.where(fechas.notna(), None)


def convertir_numero(serie: pd.Series) -> pd.Series:
    """'$1,234.50' → 1234.5 ; vacío o inválido → None."""
    texto = serie.astype("string").str.replace(r"[\$,\s]", "", regex=True)
    numeros = pd.to_numeric(texto, errors="coerce").astype("float64")
    return numeros.astype(object).where(numeros.notna(), None)


# ===========================================================================
# 5. Mapeo al esquema SAP
# ===========================================================================
def mapear_a_sap(df: pd.DataFrame) -> pd.DataFrame:
    """
    Transforma el DataFrame al esquema de la UDT de SAP B1:
      1. Valida que existan las columnas obligatorias.
      2. Conserva SOLO las columnas mapeadas (el resto se descarta).
      3. Fechas → ISO YYYY-MM-DD (NaT → None); importes → float (NaN → None).
      4. Renombra a los campos SAP.
      5. Inserta 'Name' = UUID (requisito de toda UDT).
    """
    faltantes = [c for c in MAPEO_COLUMNAS if c not in df.columns]
    if faltantes:
        raise ErrorValidacion(
            f"Columnas obligatorias faltantes: {faltantes}. "
            f"El archivo debe contener: {list(MAPEO_COLUMNAS)}."
        )

    mapeo = dict(MAPEO_COLUMNAS)
    mapeo.update({k: v for k, v in MAPEO_OPCIONAL.items() if k in df.columns})

    df_sap = df[list(mapeo)].copy().astype(object)

    for col in COLUMNAS_FECHA:
        if col in df_sap.columns:
            df_sap[col] = convertir_fecha_iso(df[col])
    for col in COLUMNAS_NUMERICAS:
        if col in df_sap.columns:
            df_sap[col] = convertir_numero(df[col])

    df_sap = df_sap.rename(columns=mapeo)
    df_sap.insert(1, "Name", df_sap["Code"])
    return df_sap


def columnas_ignoradas(df: pd.DataFrame) -> list[str]:
    """Columnas del archivo que NO se enviarán a SAP (informativo)."""
    usadas = set(MAPEO_COLUMNAS) | set(MAPEO_OPCIONAL)
    return [c for c in df.columns if c not in usadas]


# ===========================================================================
# 6. Payload JSON
# ===========================================================================
def _valor_nativo(valor):
    """Convierte NaN/NA/NaT a None y tipos numpy a tipos nativos de Python."""
    if valor is None:
        return None
    try:
        if pd.isna(valor):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(valor, "item"):          # numpy.float64, numpy.int64…
        valor = valor.item()
    if isinstance(valor, float) and math.isinf(valor):
        return None
    return valor


def a_payload(df: pd.DataFrame) -> list[dict]:
    """
    df.to_dict(orient='records') garantizando JSON válido para SAP:
    ningún NaN/NaT (todos → null) y solo tipos nativos de Python.
    """
    return [
        {k: _valor_nativo(v) for k, v in registro.items()}
        for registro in df.to_dict(orient="records")
    ]


# ===========================================================================
# 7. Varios archivos de la misma empresa (p. ej. Facturas + Notas de Crédito)
# ===========================================================================
def procesar_archivo(archivo, nombre_archivo: str, rfc_esperado: str) -> dict:
    """
    Lee, valida el RFC, limpia y mapea UN archivo.
    Los errores se relanzan con el nombre del archivo para que la contadora
    sepa exactamente cuál revisar.
    """
    try:
        df_raw = leer_archivo(archivo, nombre_archivo)
        columna_rfc = validar_rfc(df_raw, rfc_esperado)
        df_base, descartes = limpiar(df_raw)
        df_sap = mapear_a_sap(df_base)
    except ErrorValidacion as exc:
        raise ErrorValidacion(f"**{nombre_archivo}**: {exc}") from exc
    return {
        "nombre": nombre_archivo,
        "columna_rfc": columna_rfc,
        "df_base": df_base,
        "df_sap": df_sap,
        "descartes": descartes,
    }


def unir_archivos(dfs_sap: list[pd.DataFrame]) -> tuple[pd.DataFrame, list[dict], int]:
    """
    Une los DataFrames (ya en formato SAP) de varios archivos de la misma empresa.

    - Payload: cada registro conserva SOLO las columnas de SU archivo. Así, un
      archivo con menos columnas (p. ej. Notas de Crédito, sin 'Forma Pago')
      no envía null en esos campos y no borra información existente en SAP.
      Resultado idéntico a subir los archivos por separado.
    - UUID repetidos entre archivos: se conserva el del último archivo.
    - Vista previa: unión de columnas de todos los archivos.

    Returns: (vista_previa, payload_list, uuids_repetidos_entre_archivos)
    """
    por_code: dict[str, dict] = {}
    for df in dfs_sap:
        for registro in a_payload(df):
            por_code[registro["Code"]] = registro
    total = sum(len(df) for df in dfs_sap)
    vista = pd.concat(dfs_sap, ignore_index=True).drop_duplicates("Code", keep="last")
    return vista.reset_index(drop=True), list(por_code.values()), total - len(por_code)
