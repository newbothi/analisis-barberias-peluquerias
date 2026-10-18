"""
Pruebas de la Fase 4 con datos sintéticos (sin SQL Server ni datos reales).
Ejecutar desde la raíz del proyecto:  python -m pytest -q
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import entrenar_modelo as em  # noqa: E402


def sinteticos(n_cuentas=6, posts=60, semilla=3):
    """Cada cuenta tiene su nivel; los reels suman 0,5 y el llamado a la acción resta 0,3 (en log)."""
    rng = np.random.default_rng(semilla)
    filas = []
    for c in range(n_cuentas):
        nivel = rng.uniform(1.5, 5.0)
        seguidores = int(np.exp(nivel + 5))
        fechas = pd.Timestamp("2025-10-01") + pd.to_timedelta(np.sort(rng.uniform(0, 360, posts)), unit="D")
        for i, f in enumerate(fechas):
            formato = rng.choice(["reel", "carrusel", "imagen"], p=[0.6, 0.25, 0.15])
            llamado = rng.random() < 0.3
            log_likes = nivel + 0.5 * (formato == "reel") - 0.3 * llamado + rng.normal(scale=0.4)
            likes = int(round(np.expm1(max(log_likes, 0.0))))       # likes enteros y log_likes = ln(1 + likes)
            filas.append({
                "post_id": f"c{c}-{i}", "username": f"cuenta{c}", "fecha_publicacion_utc": f,
                "log_likes": np.log1p(likes), "likes": likes, "formato": formato,
                "franja": rng.choice(["manana", "tarde", "noche"]), "log_seguidores": np.log(seguidores),
                "tiene_llamado_accion": llamado, "menciona_precio_promo": rng.random() < 0.05,
                "tiene_pregunta": rng.random() < 0.15, "es_fin_de_semana": f.dayofweek >= 5,
                "largo_texto_propio": int(rng.integers(0, 300)), "hashtags_propios": int(rng.integers(0, 6)),
                "emojis_propios": int(rng.integers(0, 5)), "posts_ultimos_7_dias": int(rng.integers(0, 6)),
            })
    return (pd.DataFrame(filas).sort_values(["fecha_publicacion_utc", "post_id"]).reset_index(drop=True))


# ------------------------------------------------------------------ features

def test_nivel_previo_usa_solo_el_pasado():
    df = pd.DataFrame({
        "username": ["a", "b", "a", "a", "b", "a"],
        "post_id": list("123456"),
        "fecha_publicacion_utc": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03",
                                                 "2026-01-05", "2026-01-06", "2026-01-09"]),
        "log_likes": [1.0, 10.0, 3.0, 5.0, 20.0, 99.0],
    })
    nivel = em.nivel_previo(df, ventana=2)
    assert np.isnan(nivel[0]) and np.isnan(nivel[1])               # primer post de cada cuenta
    assert nivel[2] == 1.0                                          # solo el post anterior de "a"
    assert nivel[3] == pytest.approx(2.0)                           # (1 + 3) / 2
    assert nivel[5] == pytest.approx(4.0)                           # ventana 2: (3 + 5) / 2, no usa el 99 propio
    assert nivel[4] == 10.0                                         # no mezcla cuentas


def test_nivel_previo_no_depende_del_orden_de_las_filas():
    df = sinteticos()
    desordenado = df.sample(frac=1, random_state=0)
    pd.testing.assert_series_equal(em.nivel_previo(df).sort_index(), em.nivel_previo(desordenado).sort_index())


def test_matriz_features():
    df = sinteticos().head(50).copy()
    df.loc[0, "formato"] = "video_feed"
    df.loc[1, "franja"] = "madrugada"
    X = em.matriz_features(df)
    assert list(X.columns) == em.FEATURES_POST
    assert X.loc[0, ["es_reel", "es_carrusel"]].sum() == 0           # video del feed va a la referencia (imagen)
    assert X.loc[1, "franja_madrugada"] == 1
    assert X.notna().all().all()


# ------------------------------------------------------------------ validación

def test_cortes_temporales_sin_solaparse():
    fechas = sinteticos()["fecha_publicacion_utc"]
    cortes = em.cortes_temporales(fechas, n_cortes=4)
    assert len(cortes) == 4
    for (a, b), (c, _) in zip(cortes[:-1], cortes[1:]):
        assert a < b == c
    assert cortes[-1][1] > fechas.max()


def test_validacion_temporal_no_mira_el_futuro():
    df = sinteticos()
    _, pred1 = em.evaluar_barberia_conocida(df, n_cortes=4)
    inicio_ultimo = em.cortes_temporales(df["fecha_publicacion_utc"], 4)[-1][0]
    alterado = df.copy()
    futuro = alterado["fecha_publicacion_utc"] >= inicio_ultimo
    alterado.loc[futuro, "log_likes"] += 5                          # cambia solo el último tramo
    _, pred2 = em.evaluar_barberia_conocida(alterado, n_cortes=4)
    antes = pred1["fecha_publicacion_utc"] < inicio_ultimo
    columnas = [c for c in pred1.columns if c not in ("likes", "log_likes", "es_viral")]
    pd.testing.assert_frame_equal(pred1.loc[antes, columnas], pred2.loc[antes, columnas])


def test_escenario_a_tabla_y_modelos_aprenden():
    tabla, pred = em.evaluar_barberia_conocida(sinteticos(), n_cortes=4)
    assert {"Mediana global", "Nivel reciente (20 posts)", "Huber (lineal robusta) + nivel"} <= set(tabla.index)
    assert tabla.loc["Huber (lineal robusta) + nivel", "MAE"] < tabla.loc["Mediana global", "MAE"]
    # el descenso de gradiente propio da lo mismo que la regresión lineal de scikit-learn
    np.testing.assert_allclose(pred["Regresión lineal (descenso de gradiente propio) + nivel"],
                               pred["Regresión lineal + nivel"], atol=1e-3)
    assert int(tabla["posts"].iloc[0]) == len(pred)


def test_escenario_b_cuentas_nuevas():
    tabla = em.evaluar_barberia_nueva(sinteticos(n_cuentas=8), n_folds=4)
    assert {"Mediana global", "Solo seguidores", "Huber (lineal robusta)"} <= set(tabla.index)
    assert np.isfinite(tabla["MAE"].astype(float)).all()


# ------------------------------------------------------------------ modelo final

def test_modelo_final_recupera_los_efectos():
    df = sinteticos(n_cuentas=8, posts=120)
    final, columnas, _ = em.entrenar_final(df, "Huber (lineal robusta)")
    coefs = em.coeficientes(final, columnas).set_index("variable")
    assert coefs.loc["es_reel", "coef_original"] == pytest.approx(0.5, abs=0.15)
    assert coefs.loc["tiene_llamado_accion", "coef_original"] == pytest.approx(-0.3, abs=0.15)
    assert coefs.loc["es_reel", "efecto_%"] == pytest.approx((np.exp(coefs.loc["es_reel", "coef_original"]) - 1) * 100)
    assert set(coefs.index) == set(em.TIPO_FEATURE)


def test_random_forest_no_tiene_coeficientes():
    final, columnas, _ = em.entrenar_final(sinteticos(), "Random Forest")
    assert em.coeficientes(final, columnas) is None


def test_main_escribe_los_archivos(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    sinteticos().to_csv(tmp_path / "data" / "posts_features.csv", index=False, encoding="utf-8-sig")
    monkeypatch.setattr(em, "RAIZ", tmp_path)
    assert em.main(["--csv", str(tmp_path / "data" / "posts_features.csv")]) == 0
    for archivo in ("data/resultados_modelos.csv", "data/predicciones_validacion.csv",
                    "modelos/modelo_final.joblib", "modelos/modelo_final.json"):
        assert (tmp_path / archivo).exists(), archivo
    meta = json.loads((tmp_path / "modelos" / "modelo_final.json").read_text(encoding="utf-8"))
    assert meta["features"][-1] == "nivel_reciente" and meta["metricas_validacion_temporal"]["MAE"] > 0


def test_main_sin_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(em, "RAIZ", tmp_path)
    assert em.main(["--csv", str(tmp_path / "no_existe.csv")]) == 2
