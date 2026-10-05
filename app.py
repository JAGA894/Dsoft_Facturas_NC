"""
app.py – Interfaz web (Streamlit) para cargar facturas del SAT a SAP B1
======================================================================
Responsabilidad única: la interfaz de usuario.
  - procesador.py → lectura, validación y transformación con pandas
  - sap_api.py    → comunicación con SAP Business One Service Layer

Ejecutar:  streamlit run app.py      (o doble clic en iniciar_app.bat)
"""

import time

import pandas as pd
import streamlit as st

from procesador import (
    ErrorValidacion,
    batch_id_valido,
    columnas_ignoradas,
    generar_batch_id,
    procesar_archivo,
    unir_archivos,
)
from sap_api import SAPClient, SAPError

# ---------------------------------------------------------------------------
# Configuración general de la página
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="DSOFT – Carga a SAP B1",
    page_icon="📊",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Catálogo de empresas: nombre visible → RFC oficial registrado en el SAT.
# El RFC del archivo debe coincidir con el de la empresa seleccionada para
# evitar cargar facturas de una razón social incorrecta a SAP.
# ---------------------------------------------------------------------------
EMPRESAS_RFC = {
    "ERSA SOLUCIONES INTEGRALES":     "ESI151228L26",
    "CONSTRUCCIONES SBA":             "CSB1201163D6",
    "ERSAGRO SOLUCIONES":             "ESO210513J13",
    "EUGENIO RICARDO STERLING ARANA": "SEAE8106183T6",
    "EUGENIO RICARDO STERLING BOURS": "SEBE541109MA5",
    "GRUPO JEUMA":                    "GJE1512285G9",
}

# Empresa → base de datos SAP (CompanyDB) donde se cargan sus facturas.
# Verificado contra SAP leyendo el RFC (FederalTaxID) de cada base.
# Cada base debe estar listada también en SAP_COMPANYDB del .env.
EMPRESAS_DB = {
    "ERSA SOLUCIONES INTEGRALES":     "ERSA_PRODUCTIVA",
    "CONSTRUCCIONES SBA":             "SBA_PROD",
    "ERSAGRO SOLUCIONES":             "ERSAGRO_PROD",
    "EUGENIO RICARDO STERLING ARANA": "SA_PRODUCTIVA",
    "EUGENIO RICARDO STERLING BOURS": "SB_PROD",
    "GRUPO JEUMA":                    "JEUMA_PRODUCTIVA",
}


def empresa_seleccionada() -> str:
    """Empresa elegida en el selector (la barra lateral se dibuja antes que él)."""
    return st.session_state.get("empresa") or next(iter(EMPRESAS_RFC))


def nuevo_cliente() -> SAPClient:
    """SAPClient conectado a la base de datos de la empresa seleccionada."""
    return SAPClient(company_db=EMPRESAS_DB[empresa_seleccionada()])


# ===========================================================================
# Acciones contra SAP (siempre con logout garantizado en finally)
# ===========================================================================
def probar_conexion() -> None:
    try:
        cliente = nuevo_cliente()
    except SAPError as exc:
        st.error(f"❌ {exc}")
        return
    try:
        cliente.login()
        st.success(f"✅ Conexión correcta con **{cliente.company_db}** (tabla @{cliente.tabla}).")
    except SAPError as exc:
        st.error(f"❌ {exc}")
    finally:
        cliente.logout()


def subir_a_sap(payload_list: list[dict]) -> dict | None:
    """
    Orquesta la carga a SAP:
      try     → login + upsert_lote con barra de progreso en vivo
      except  → muestra el error crítico sin romper la UI
      finally → logout() SIEMPRE, para liberar la licencia SAP.
    """
    barra = st.progress(0.0, text="Iniciando conexión con SAP…")
    estado = st.empty()
    inicio = time.monotonic()

    try:
        cliente = nuevo_cliente()
    except SAPError as exc:
        barra.empty()
        st.error(f"❌ {exc}")
        return None

    try:
        estado.info("🔐 Autenticando en SAP Business One…")
        cliente.login()

        def actualizar_progreso(actual: int, total: int) -> None:
            barra.progress(actual / total if total else 1.0,
                           text=f"Procesando registro {actual} de {total}…")

        resultado = cliente.upsert_lote(
            payload_list,
            progress_callback=actualizar_progreso,
            mensaje_callback=estado.info,
        )
        resultado["segundos"] = time.monotonic() - inicio
        barra.progress(1.0, text="¡Proceso completado!")
        estado.empty()
        return resultado

    except SAPError as exc:
        barra.empty()
        estado.empty()
        st.error(f"❌ Error crítico durante la carga a SAP: {exc}")
        return None

    finally:
        # Un logout omitido deja la licencia ocupada hasta que SAP la recicle
        # por timeout, impidiendo que otros usuarios inicien sesión.
        cliente.logout()


def mostrar_resultado(res: dict, batch_id: str) -> None:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("✅ Creados", res["creados"])
    c2.metric("🔄 Actualizados", res["actualizados"])
    c3.metric("⏭️ Sin cambios", res.get("sin_cambios", 0),
              help="Ya estaban en SAP con los mismos datos; no se reenviaron.")
    c4.metric("❌ Errores", res["errores"])

    minutos, segundos = divmod(int(res.get("segundos", 0)), 60)
    duracion = f" en **{minutos} min {segundos} s**" if "segundos" in res else ""
    if res["errores"] == 0:
        st.success(
            f"🎉 Lote **{batch_id}** procesado sin errores{duracion}. "
            f"Creados: **{res['creados']}** | Actualizados: **{res['actualizados']}** | "
            f"Sin cambios: **{res.get('sin_cambios', 0)}**"
        )
    else:
        st.warning(f"⚠️ Lote **{batch_id}** procesado{duracion} con **{res['errores']} error(es)**.")
        with st.expander(f"🔍 Ver detalle de {res['errores']} error(es)", expanded=True):
            for msg in res["detalle_errores"]:
                st.error(msg)

    st.caption("Guarda este Batch ID por si necesitas revertir la carga:")
    st.code(batch_id, language=None)


def revertir_lote(batch_id: str) -> None:
    try:
        cliente = nuevo_cliente()
    except SAPError as exc:
        st.error(f"❌ {exc}")
        return

    barra = st.progress(0.0, text="Buscando registros del lote…")
    try:
        cliente.login()

        def avance(actual: int, total: int) -> None:
            barra.progress(actual / total, text=f"Eliminando {actual} de {total}…")

        res = cliente.eliminar_lote(batch_id, progress_callback=avance)
        barra.empty()
        if res["eliminados"] == 0 and res["errores"] == 0:
            st.info(f"No se encontraron registros creados por **{batch_id}**.")
        elif res["errores"] == 0:
            st.success(f"✅ Lote revertido: **{res['eliminados']}** registro(s) eliminados.")
        else:
            st.warning(f"Eliminados: {res['eliminados']} | Errores: {res['errores']}")
            with st.expander("Detalle de errores"):
                for msg in res["detalle_errores"]:
                    st.error(msg)
    except SAPError as exc:
        barra.empty()
        st.error(f"❌ {exc}")
    finally:
        cliente.logout()


# ===========================================================================
# BARRA LATERAL: conexión y rollback
# ===========================================================================
with st.sidebar:
    st.header("⚙️ Herramientas")
    st.caption(f"Empresa activa: **{empresa_seleccionada()}** → base **{EMPRESAS_DB[empresa_seleccionada()]}**")
    if st.button("🔌 Probar conexión con SAP", width="stretch"):
        probar_conexion()

    st.divider()
    st.subheader("🧯 Revertir un lote")
    st.caption(
        "Elimina de SAP **solo los registros que ese lote CREÓ**. "
        "Las facturas que ya existían y solo se actualizaron no se borran."
    )
    lote = st.text_input(
        "Batch ID a revertir",
        value=st.session_state.get("ultimo_lote_subido", ""),
        placeholder="LOTE_20261005_0830",
    ).strip()
    confirmado = st.checkbox("Confirmo que quiero eliminar este lote de SAP")
    if st.button(
        "🗑️ Eliminar lote",
        width="stretch",
        disabled=not (confirmado and batch_id_valido(lote)),
    ):
        revertir_lote(lote)
    if lote and not batch_id_valido(lote):
        st.caption("⚠️ Formato esperado: LOTE_YYYYMMDD_HHMM")


# ===========================================================================
# UI PRINCIPAL
# ===========================================================================
st.title("📊 DSOFT – Carga de Facturas SAT a SAP B1")
st.markdown("Selecciona la empresa, sube el reporte del SAT (**CSV** o **Excel**) y envíalo a SAP Business One.")
st.divider()

empresa = st.selectbox(
    "🏢 1. Selecciona la empresa",
    options=list(EMPRESAS_RFC.keys()),
    help="El RFC de esta empresa debe coincidir con el del archivo.",
    key="empresa",
)
st.caption(f"Base de datos SAP destino: **{EMPRESAS_DB[empresa]}**")

archivos = st.file_uploader(
    "📁 2. Sube los reportes del SAT de esta empresa (puedes seleccionar varios a la vez)",
    type=["csv", "xlsx"],
    accept_multiple_files=True,
    help=(
        "Ejemplo: 'FACTURAS RECIBIDAS' y 'NC RECIBIDAS' juntos. Cada archivo se valida "
        "por separado y todos se envían en un solo lote. Las primeras 4 filas de cada "
        "archivo (encabezado del reporte) se omiten automáticamente."
    ),
)

if not archivos:
    st.info("⬆️ Sube uno o varios archivos para comenzar.")
    st.stop()

# ── Lectura, validación y transformación (procesador.py) ────────────────────
procesados, errores = [], []
for archivo in archivos:
    try:
        procesados.append(procesar_archivo(archivo, archivo.name, EMPRESAS_RFC[empresa]))
    except ErrorValidacion as exc:
        errores.append(str(exc))
    except Exception as exc:  # archivo corrupto, formato inesperado, etc.
        errores.append(f"**{archivo.name}**: No se pudo procesar el archivo: {exc}")

if errores:
    for error in errores:
        st.error(f"❌ {error}")
    st.stop()

df_sap, payload_list, repetidos_entre_archivos = unir_archivos([p["df_sap"] for p in procesados])

st.success(f"✅ RFC verificado en {len(procesados)} archivo(s) → **{empresa}**")

# ── Trazabilidad – Batch ID para Rollback ───────────────────────────────────
# Se envía a SAP en U_BatchID. Si hubo un error humano, el lote se revierte
# desde la barra lateral (DELETE masivo de los registros con ese U_BatchID).
# Se guarda en session_state para que no cambie entre recargas de la página
# mientras se trabaja con los mismos archivos. Un solo lote para todos ellos.
clave_archivo = "|".join(sorted(f"{a.name}:{a.size}" for a in archivos)) + f"|{empresa}"
if st.session_state.get("clave_archivo") != clave_archivo:
    st.session_state["clave_archivo"] = clave_archivo
    st.session_state["batch_id"] = generar_batch_id()
    st.session_state.pop("resultado", None)
batch_id = st.session_state["batch_id"]
df_sap["U_BatchID"] = batch_id
for registro in payload_list:
    registro["U_BatchID"] = batch_id

# ── Métricas ────────────────────────────────────────────────────────────────
sin_uuid = sum(p["descartes"]["sin_uuid"] for p in procesados)
duplicados = sum(p["descartes"]["duplicados"] for p in procesados)
c1, c2, c3, c4 = st.columns(4)
c1.metric("📄 Registros a enviar", len(payload_list))
c2.metric("📁 Archivos", len(procesados))
c3.metric("🚫 Descartados", sin_uuid + duplicados + repetidos_entre_archivos)
c4.metric("🔖 Batch ID", batch_id)

st.dataframe(
    pd.DataFrame([
        {
            "Archivo": p["nombre"],
            "Registros": len(p["df_sap"]),
            "Campos SAP": len(p["df_sap"].columns),
            "RFC verificado en": p["columna_rfc"],
        }
        for p in procesados
    ]),
    width="stretch",
    hide_index=True,
)

if sin_uuid:
    st.warning(f"Se omitieron {sin_uuid} fila(s) sin UUID.")
if duplicados:
    st.warning(f"Se omitieron {duplicados} UUID(s) repetido(s) dentro de un mismo archivo (se conservó el último).")
if repetidos_entre_archivos:
    st.warning(f"{repetidos_entre_archivos} UUID(s) venían en más de un archivo; se conservó el del último archivo.")
for p in procesados:
    ignoradas = columnas_ignoradas(p["df_base"])
    if ignoradas:
        with st.expander(f"ℹ️ {p['nombre']}: {len(ignoradas)} columna(s) no se envían a SAP"):
            st.write(", ".join(ignoradas))

st.subheader("📋 3. Revisa los datos (ya en formato SAP)")
st.dataframe(df_sap, width="stretch", hide_index=True)

estatus = pd.concat([p["df_base"]["Estatus"] for p in procesados if "Estatus" in p["df_base"].columns])
if not estatus.empty:
    st.caption(
        "Estatus incluidos: "
        + ", ".join(f"{k}: {v}" for k, v in estatus.value_counts(dropna=False).items())
    )

st.divider()
col_a, col_b = st.columns(2)

with col_a:
    if st.button(
        "🔍 Generar Payload de Prueba",
        width="stretch",
        disabled=not payload_list,
        help="Muestra los primeros 3 registros en el JSON exacto que recibe SAP.",
    ):
        st.json(payload_list[:3])

with col_b:
    if st.button(
        "🚀 4. Subir datos a SAP B1",
        type="primary",
        width="stretch",
        disabled=not payload_list,
    ):
        resultado = subir_a_sap(payload_list)
        if resultado is not None:
            st.session_state["resultado"] = resultado
            st.session_state["ultimo_lote_subido"] = batch_id

if "resultado" in st.session_state:
    st.divider()
    st.subheader("📦 Resultado de la carga")
    mostrar_resultado(st.session_state["resultado"], batch_id)
