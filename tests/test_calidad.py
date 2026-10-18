"""
Pruebas del control de calidad: validación de datos (calidad/validar_datos.py) y control del modelo
(calidad/controlar_modelo.py), y su integración con preparar_features.py y entrenar_modelo.py.
Ejecutar desde la raíz del proyecto:  python -m pytest -q
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import entrenar_modelo as em  # noqa: E402
import preparar_features as pf  # noqa: E402
from calidad import controlar_modelo as cm  # noqa: E402
from calidad import validar_datos as vd  # noqa: E402
from test_entrenar_modelo import sinteticos  # noqa: E402
from test_preparar_features import cuenta  # noqa: E402


def tabla_valida() -> pd.DataFrame:
    filas = cuenta("a", 25) + cuenta("b", 25, seguidores=8000, media="CAROUSEL_ALBUM", producto="FEED")
    return pf.construir_features(pd.DataFrame(filas), min_posts=20).posts


def validar(df, **kw):
    return vd.validar_posts(df, columnas_requeridas=list(pf.COLUMNAS_SALIDA), **kw)


def estado(validacion, nombre):
    return next(r.estado for r in validacion.reglas if r.nombre == nombre)


# ------------------------------------------------------------------ validación de datos

def test_la_salida_de_preparar_features_pasa_la_validacion():
    v = validar(tabla_valida(), min_posts=20, meses=12, min_edad_dias=7)
    assert v.ok, v.lineas_problemas()
    assert not [r for r in v.reglas if r.estado == vd.OMITIDA]
    assert {r.nombre for r in v.avisos} == {"n_cuentas", "concentracion"}     # solo 2 cuentas en el ejemplo


def test_tambien_pasa_despues_de_guardar_y_leer_el_csv(tmp_path):
    tabla_valida().to_csv(tmp_path / "posts.csv", index=False, encoding="utf-8-sig")
    leida = pd.read_csv(tmp_path / "posts.csv", encoding="utf-8-sig", dtype={"post_id": str})
    assert validar(leida, min_posts=20, meses=12, min_edad_dias=7).ok


def _poner(columna, valor, fila=0):
    def cambiar(df):
        df.loc[fila, columna] = valor
    return cambiar


CORRUPCIONES = {
    "post_id": lambda df: df.__setitem__("post_id", ["mismo"] * len(df)),
    "cuenta": _poner("username", None),
    "fechas": _poner("fecha_publicacion_utc", pd.Timestamp("2099-01-01")),
    "likes": _poner("likes", -3),
    "log_likes": lambda df: df.__setitem__("log_likes", df["log_likes"] + 0.5),
    "seguidores": lambda df: df.__setitem__("seguidores", 0),
    "hora": _poner("hora_local", 25),
    "dia": lambda df: df.__setitem__("es_fin_de_semana", ~df["es_fin_de_semana"]),
    "franja": lambda df: (_poner("franja", "noche")(df), _poner("hora_local", 9)(df)),
    "conteos": _poner("emojis_propios", -1),
    "booleanas": lambda df: df.__setitem__("tiene_pregunta", ["quizás"] * len(df)),
    "ritmo": _poner("dias_desde_post_anterior", -2.0, fila=3),
    "engagement": _poner("engagement_rate", np.inf),
    "peso": lambda df: df.__setitem__("peso_cuenta", df["peso_cuenta"] * 2),
    "edad": _poner("edad_dias_captura", 1.5),
    "ventana": _poner("edad_dias_captura", 500.0),
}


@pytest.mark.parametrize("regla", sorted(CORRUPCIONES))
def test_cada_regla_detecta_su_problema(regla):
    df = tabla_valida()
    df["tiene_pregunta"] = df["tiene_pregunta"].astype(object)
    df["fecha_publicacion_utc"] = df["fecha_publicacion_utc"].astype(object)
    CORRUPCIONES[regla](df)
    v = validar(df, min_posts=20, meses=12, min_edad_dias=7)
    assert estado(v, regla) == vd.ERROR
    assert not v.ok


def test_hashtags_propios_no_pueden_superar_al_total():
    df = tabla_valida()
    df.loc[0, "hashtags_propios"] = df.loc[0, "hashtags_total"] + 1
    assert estado(validar(df), "conteos") == vd.ERROR


def test_faltan_columnas():
    v = validar(tabla_valida().drop(columns=["franja"]))
    assert estado(v, "columnas") == vd.ERROR
    assert estado(v, "franja") == vd.OMITIDA


def test_cuentas_bajo_el_minimo():
    assert estado(validar(tabla_valida(), min_posts=30), "min_posts") == vd.ERROR


def test_un_aviso_no_bloquea():
    df = tabla_valida()
    df.loc[0, "formato"] = "otro"
    v = validar(df)
    assert estado(v, "formato") == vd.AVISO and v.ok


def test_tabla_vacia():
    v = validar(tabla_valida().head(0))
    assert not v.ok and estado(v, "filas") == vd.ERROR


def test_reporte(tmp_path):
    df = tabla_valida()
    df.loc[0, "likes"] = -1
    ruta = vd.guardar_reporte(validar(df), "prueba", tmp_path)
    texto = ruta.read_text(encoding="utf-8")
    assert "RECHAZADA" in texto and "Likes enteros" in texto and str(df.loc[0, "post_id"]) in texto


def test_preparar_features_no_guarda_si_los_datos_no_pasan(tmp_path, monkeypatch):
    carpeta = tmp_path / "salida"
    carpeta.mkdir()
    monkeypatch.setattr(pf, "RAIZ", tmp_path)
    monkeypatch.setattr(pf, "cargar_desde_jsonl", lambda _: pd.DataFrame(cuenta("a", 25)))
    original = pf.construir_features

    def con_ids_repetidos(*args, **kw):
        res = original(*args, **kw)
        res.posts["post_id"] = "repetido"
        return res

    monkeypatch.setattr(pf, "construir_features", con_ids_repetidos)
    assert pf.main(["--desde-jsonl", str(carpeta), "--min-posts", "20"]) == 3
    assert not (carpeta / "posts_features.csv").exists()
    assert list((tmp_path / "reportes" / "calidad").glob("validacion_features_*.md"))

    monkeypatch.setattr(pf, "construir_features", original)
    assert pf.main(["--desde-jsonl", str(carpeta), "--min-posts", "20"]) == 0
    assert (carpeta / "posts_features.csv").exists()


# ------------------------------------------------------------------ control del modelo

BUENO = {"MAE": 0.68, "R2": 0.40}
REFERENCIA = {"MAE": 0.70, "R2": 0.38}
PREDICCIONES = np.array([1.2, 3.4, 5.0])


def test_control_aprueba_un_modelo_bueno():
    control = cm.controlar_modelo(BUENO, REFERENCIA, PREDICCIONES, coeficientes=np.array([0.5, -0.2]))
    assert control.aprobado
    assert [c.nombre for c in control.chequeos] == ["supera_referencia", "explica_variacion", "no_empeora",
                                                     "predicciones_validas", "coeficientes_finitos"]


@pytest.mark.parametrize("metricas, referencia, predicciones, anterior, coefs, falla", [
    ({"MAE": 0.72, "R2": 0.4}, REFERENCIA, PREDICCIONES, None, None, "supera_referencia"),
    ({"MAE": 0.68, "R2": -0.1}, REFERENCIA, PREDICCIONES, None, None, "explica_variacion"),
    (BUENO, REFERENCIA, PREDICCIONES, {"metricas_validacion_temporal": {"MAE": 0.60}}, None, "no_empeora"),
    (BUENO, REFERENCIA, np.array([1.0, np.nan]), None, None, "predicciones_validas"),
    (BUENO, REFERENCIA, np.array([1.0, 50.0]), None, None, "predicciones_validas"),
    (BUENO, REFERENCIA, PREDICCIONES, None, np.array([0.1, np.inf]), "coeficientes_finitos"),
])
def test_control_rechaza(metricas, referencia, predicciones, anterior, coefs, falla):
    control = cm.controlar_modelo(metricas, referencia, predicciones, anterior, coefs)
    assert not control.aprobado
    assert [c.nombre for c in control.fallidos] == [falla]


def test_empeorar_dentro_de_la_tolerancia_esta_permitido():
    anterior = {"metricas_validacion_temporal": {"MAE": 0.66}}               # 0,68 es un 3% peor
    assert cm.controlar_modelo(BUENO, REFERENCIA, PREDICCIONES, anterior).aprobado


def test_entrenar_modelo_no_reemplaza_el_modelo_si_falla_el_control(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    csv = tmp_path / "data" / "posts_features.csv"
    sinteticos().to_csv(csv, index=False, encoding="utf-8-sig")
    monkeypatch.setattr(em, "RAIZ", tmp_path)
    ruta_meta = tmp_path / "modelos" / "modelo_final.json"

    assert em.main(["--csv", str(csv)]) == 0
    meta = json.loads(ruta_meta.read_text(encoding="utf-8"))
    assert all(c["aprobado"] for c in meta["control_calidad"])

    meta["metricas_validacion_temporal"]["MAE"] = 0.01      # simula un modelo anterior mucho mejor
    ruta_meta.write_text(json.dumps(meta), encoding="utf-8")
    assert em.main(["--csv", str(csv)]) == 4
    assert json.loads(ruta_meta.read_text(encoding="utf-8"))["metricas_validacion_temporal"]["MAE"] == 0.01
    rechazado = json.loads((tmp_path / "modelos" / "candidato_rechazado.json").read_text(encoding="utf-8"))
    assert [c["nombre"] for c in rechazado["control_calidad"] if not c["aprobado"]] == ["no_empeora"]

    assert em.main(["--csv", str(csv), "--aceptar-modelo"]) == 0
    meta = json.loads(ruta_meta.read_text(encoding="utf-8"))
    assert meta["aceptado_manualmente"] and meta["metricas_validacion_temporal"]["MAE"] > 0.01
    assert not (tmp_path / "modelos" / "candidato_rechazado.json").exists()


def test_entrenar_modelo_no_entrena_con_datos_invalidos(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    datos = sinteticos()
    datos.loc[5, "likes"] = -10
    csv = tmp_path / "data" / "posts_features.csv"
    datos.to_csv(csv, index=False, encoding="utf-8-sig")
    monkeypatch.setattr(em, "RAIZ", tmp_path)
    assert em.main(["--csv", str(csv)]) == 3
    assert not (tmp_path / "modelos" / "modelo_final.json").exists()
    assert list((tmp_path / "reportes" / "calidad").glob("validacion_modelo_*.md"))
