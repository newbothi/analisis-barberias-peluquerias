"""
Pruebas de la Fase 5 (interpretación, anonimización y datos del dashboard) con datos sintéticos.
Ejecutar desde la raíz del proyecto:  python -m pytest -q
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import anonimizar as an  # noqa: E402
import interpretar_modelo as im  # noqa: E402
import preparar_dashboard as pdash  # noqa: E402
from test_entrenar_modelo import sinteticos  # noqa: E402


# ------------------------------------------------------------------ anonimizar

def test_mapa_cuentas_ordena_por_cantidad_de_posts():
    usuarios = pd.Series(["b", "a", "b", "c", "c", "b", "a"])
    assert an.mapa_cuentas(usuarios) == {"b": "Barbería 01", "a": "Barbería 02", "c": "Barbería 03"}


def test_mapa_cuentas_es_estable_ante_el_orden_de_las_filas():
    usuarios = pd.Series(list("aabbbcdd"))
    assert an.mapa_cuentas(usuarios) == an.mapa_cuentas(usuarios.sample(frac=1, random_state=1))


def test_anonimizar_quita_textos_y_nombres():
    df = pd.DataFrame({"username": ["juan.barber", "otra"], "texto_propio": ["gracias @cliente", "hola"],
                       "permalink": ["https://instagram.com/p/x", "y"], "likes": [3, 4]})
    salida = an.anonimizar(df)
    assert list(salida.columns) == ["username", "likes"]
    assert "juan.barber" not in salida.to_csv()
    assert df.loc[0, "username"] == "juan.barber"                    # no modifica el original


# ------------------------------------------------------------------ interpretación

def test_remuestrear_cuentas_trata_repetidas_como_distintas():
    df = sinteticos(n_cuentas=4, posts=10)
    muestra = im.remuestrear_cuentas(df, np.random.default_rng(0))
    assert muestra["username"].nunique() == 4                         # 4 "cuentas" aunque se repita alguna
    assert len(muestra) == 40
    assert muestra["fecha_publicacion_utc"].is_monotonic_increasing


def test_bootstrap_recupera_los_efectos_sinteticos():
    df = sinteticos(n_cuentas=8, posts=60)
    boot = im.bootstrap_coeficientes(df, "Huber (lineal robusta)", repeticiones=40)
    tabla = im.resumir_coeficientes(df, "Huber (lineal robusta)", boot).set_index("variable")

    reel = tabla.loc["es_reel"]
    assert reel["conclusion"] == "aumenta los likes"                 # en los datos sintéticos suma 0,5 en log
    assert reel["efecto_bajo_%"] < reel["efecto_%"] < reel["efecto_alto_%"]
    assert tabla.loc["tiene_llamado_accion", "conclusion"] == "reduce los likes"   # resta 0,3
    assert tabla.loc["tiene_pregunta", "conclusion"] == "sin evidencia suficiente"  # no tiene efecto
    assert tabla.loc["tiene_pregunta", "recomendacion"] == "No hay evidencia para decidir por esto."
    assert set(tabla.index) == set(im.em.FEATURES_POST + [im.em.FEATURE_NIVEL])


def test_bootstrap_es_reproducible():
    df = sinteticos(n_cuentas=5, posts=30)
    a = im.bootstrap_coeficientes(df, "Huber (lineal robusta)", repeticiones=5, semilla=7)
    b = im.bootstrap_coeficientes(df, "Huber (lineal robusta)", repeticiones=5, semilla=7)
    pd.testing.assert_frame_equal(a, b)


def test_bootstrap_rechaza_modelos_no_lineales():
    with pytest.raises(ValueError):
        im.bootstrap_coeficientes(sinteticos(n_cuentas=4, posts=20), "Random Forest", repeticiones=1)


def test_efectos_dentro_de_cuentas():
    df = sinteticos(n_cuentas=8, posts=60)
    tabla = im.efectos_dentro_de_cuentas(df, n_boot=500).set_index("comparacion")
    reel = tabla.loc["Reel vs no reel"]
    assert reel["señal"] == "clara" and reel["ic_bajo_%"] > 0
    assert reel["cuentas"] <= 8
    assert tabla.loc["Con llamado a la acción vs sin", "ic_alto_%"] < 0


# ------------------------------------------------------------------ dashboard

def _para_dashboard(df):
    df = df.copy()
    fecha = pd.to_datetime(df["fecha_publicacion_utc"])
    df["fecha_local"] = fecha - pd.Timedelta(hours=3)
    df["dia_semana"] = df["fecha_local"].dt.dayofweek
    df["nombre_dia"] = df["dia_semana"].map(dict(enumerate(["lunes", "martes", "miércoles", "jueves", "viernes",
                                                            "sábado", "domingo"])))
    df["hora_local"] = df["fecha_local"].dt.hour
    df["comentarios"] = 1
    df["seguidores"] = np.exp(df["log_seguidores"]).round().astype(int)
    df["engagement_rate"] = df["likes"] / df["seguidores"]
    df["texto_propio"] = "gracias @cliente.privado"
    return df


def test_tabla_posts_anonima_y_sin_textos():
    df = _para_dashboard(sinteticos(n_cuentas=3, posts=15))
    mapa = an.mapa_cuentas(df["username"])
    t = pdash.tabla_posts(df, mapa)
    assert len(t) == len(df) and t["id_post"].is_unique
    assert set(t["cuenta"]) == set(mapa.values())
    csv = t.to_csv()
    assert "cuenta0" not in csv and "cliente.privado" not in csv and "post_id" not in t.columns
    assert t["orden_dia"].between(1, 7).all()
    assert set(t["viral"].unique()) <= {0, 1}


def test_tabla_predicciones_usa_los_mismos_ids_que_posts():
    df = _para_dashboard(sinteticos(n_cuentas=3, posts=15))
    mapa = an.mapa_cuentas(df["username"])
    posts_bi = pdash.tabla_posts(df, mapa)
    pred = df.tail(10)[["post_id", "username", "fecha_publicacion_utc", "formato", "likes", "log_likes"]].assign(
        es_viral=False, **{pdash.MODELO_PRINCIPAL: 3.0, pdash.REFERENCIA: 2.5})
    t = pdash.tabla_predicciones(pred, posts_bi, df, mapa)
    assert t["id_post"].notna().all()
    unidos = t.merge(posts_bi, on="id_post", suffixes=("", "_post"))
    assert (unidos["likes"] == unidos["likes_post"]).all()
    assert (unidos["cuenta"] == unidos["cuenta_post"]).all()


def test_tabla_embudo():
    resumen = pd.DataFrame({"incluida": [True, False, True], "posts_extraidos": [50, 20, 30],
                            "posts_en_ventana": [40, 0, 25], "posts_validos": [35, 0, 20]})
    lista = pd.DataFrame({"username": ["a", "@B", None, "b"]})
    t = pdash.tabla_embudo(resumen, lista).set_index("paso")["cantidad"]
    assert t["Barberías en la lista"] == 4
    assert t["Con Instagram identificado"] == 2                       # "@B" y "b" son la misma cuenta
    assert t["Incluidas en el análisis"] == 2
    assert t["Usados en el modelo"] == 55
