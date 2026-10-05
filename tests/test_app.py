"""
Prueba de humo de la interfaz con streamlit.testing (sin conexión a SAP).
"""

from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / "app.py")


def test_app_arranca_sin_errores():
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert len(at.selectbox) == 1
    assert "ERSA SOLUCIONES INTEGRALES" in at.selectbox[0].options
    assert any("Sube uno o varios archivos" in i.value for i in at.info)


def test_boton_eliminar_lote_bloqueado_sin_confirmacion():
    at = AppTest.from_file(APP, default_timeout=30).run()
    boton = next(b for b in at.sidebar.button if "Eliminar lote" in b.label)
    assert boton.disabled
