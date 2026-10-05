"""
Pruebas de la selección de base de datos en SAPClient (sin conexión a SAP).
"""

import pytest

from sap_api import SAPClient, SAPError

BASES = "SBA_PROD,ERSA_PRODUCTIVA, SA_PRODUCTIVA ,JEUMA_PRODUCTIVA"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("SAP_URL", "https://sap.ejemplo/b1s/v1")
    monkeypatch.setenv("SAP_USER", "usuario")
    monkeypatch.setenv("SAP_PASSWORD", "Clave$123")
    monkeypatch.setenv("SAP_UDT", "FACTURAS")
    return monkeypatch


def test_lista_de_bases_usa_la_indicada(env):
    env.setenv("SAP_COMPANYDB", BASES)
    assert SAPClient(company_db="SA_PRODUCTIVA").company_db == "SA_PRODUCTIVA"


def test_lista_de_bases_sin_indicar_falla(env):
    env.setenv("SAP_COMPANYDB", BASES)
    with pytest.raises(SAPError, match="varias bases"):
        SAPClient()


def test_base_fuera_de_la_lista_falla(env):
    env.setenv("SAP_COMPANYDB", BASES)
    with pytest.raises(SAPError, match="no está en SAP_COMPANYDB"):
        SAPClient(company_db="OTRA_BASE")


def test_una_sola_base_sigue_funcionando(env):
    env.setenv("SAP_COMPANYDB", "SA_PRODUCTIVA")
    assert SAPClient().company_db == "SA_PRODUCTIVA"
    assert SAPClient(company_db="SA_PRODUCTIVA").company_db == "SA_PRODUCTIVA"


def test_app_mapea_todas_las_empresas_a_una_base():
    import ast
    from pathlib import Path

    arbol = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
    dicts = {
        n.targets[0].id: ast.literal_eval(n.value)
        for n in arbol.body
        if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") in ("EMPRESAS_RFC", "EMPRESAS_DB")
    }
    assert set(dicts["EMPRESAS_RFC"]) == set(dicts["EMPRESAS_DB"])
