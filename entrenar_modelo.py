"""
analisis-barberias-peluquerias — Fase 4: modelo

Entrena y compara modelos que predicen log_likes = ln(1 + likes) de un post, en dos escenarios:

  A. Barbería conocida: ya hay posts anteriores de la cuenta. Validación temporal (se entrena con el pasado y se
     evalúa con posts posteriores, en 4 cortes de tiempo). Usa la feature `nivel_reciente`: el promedio de
     log_likes de los 20 posts anteriores de la misma cuenta (solo pasado, sin mirar el futuro).
  B. Barbería nueva: la cuenta no aparece en el entrenamiento. Validación GroupKFold por cuenta.

Lee data/posts_features.csv (Fase 2) y escribe:
  data/resultados_modelos.csv       comparación de modelos en ambos escenarios
  data/predicciones_validacion.csv  predicciones fuera de muestra del escenario A (para la Fase 5)
  data/coeficientes_modelo.csv      coeficientes del modelo final, en escala original
  modelos/modelo_final.joblib       modelo final entrenado con todos los datos
  modelos/modelo_final.json         métricas, features y parámetros del modelo final

Control de calidad: antes de entrenar valida los datos (calidad/validar_datos.py) y antes de guardar el modelo final
comprueba que le gane a la referencia y no empeore respecto del modelo anterior (calidad/controlar_modelo.py). Si no
pasa, no reemplaza el modelo guardado: deja modelos/candidato_rechazado.json y termina con código 4.

Uso:
    python entrenar_modelo.py
    python entrenar_modelo.py --ventana 20 --cortes 4 --folds-cuenta 5
    python entrenar_modelo.py --aceptar-modelo        # guarda el modelo aunque falle el control (revisado a mano)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import warnings
from datetime import datetime
from pathlib import Path
from typing import Callable

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import RandomForestRegressor
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import HuberRegressor, LinearRegression, RidgeCV
from sklearn.model_selection import GroupKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from calidad.controlar_modelo import controlar_modelo
from calidad.validar_datos import guardar_reporte, validar_posts
from regresion_gd import RegresionLinealGD

RAIZ = Path(__file__).resolve().parent
SEMILLA = 42
Z_VIRAL = 3.5

# Features del post (se conocen antes de publicar) y la del nivel de la cuenta
FEATURES_POST = [
    "log_seguidores", "es_reel", "es_carrusel", "tiene_llamado_accion", "menciona_precio_promo", "tiene_pregunta",
    "es_fin_de_semana", "franja_tarde", "franja_noche", "franja_madrugada", "log_largo_texto", "log_hashtags",
    "log_emojis", "posts_ultimos_7_dias",
]
FEATURE_NIVEL = "nivel_reciente"

# Cómo leer cada coeficiente en la escala original (para data/coeficientes_modelo.csv)
TIPO_FEATURE = {
    "log_seguidores": ("log", "el doble de seguidores"),
    "es_reel": ("binaria", "reel en vez de imagen"),
    "es_carrusel": ("binaria", "carrusel en vez de imagen"),
    "tiene_llamado_accion": ("binaria", "tiene llamado a la acción"),
    "menciona_precio_promo": ("binaria", "menciona precio o promoción"),
    "tiene_pregunta": ("binaria", "tiene pregunta"),
    "es_fin_de_semana": ("binaria", "publicado en fin de semana"),
    "franja_tarde": ("binaria", "tarde (12-18 h) en vez de mañana"),
    "franja_noche": ("binaria", "noche (18-24 h) en vez de mañana"),
    "franja_madrugada": ("binaria", "madrugada (0-6 h) en vez de mañana"),
    "log_largo_texto": ("log", "el doble de largo de texto propio"),
    "log_hashtags": ("log", "el doble de hashtags propios"),
    "log_emojis": ("log", "el doble de emojis propios"),
    "posts_ultimos_7_dias": ("conteo", "un post más en los 7 días previos"),
    FEATURE_NIVEL: ("nivel", "+0,1 en el log_likes promedio reciente de la cuenta"),
}

log = logging.getLogger("modelo")


class ErrorFatal(Exception):
    pass


# --------------------------------------------------------------------------------------------
# Datos y features
# --------------------------------------------------------------------------------------------

def cargar(ruta: Path) -> pd.DataFrame:
    if not ruta.exists():
        raise ErrorFatal(f"No existe {ruta}. Corre primero la Fase 2 (preparar_features.py).")
    df = pd.read_csv(ruta, encoding="utf-8-sig", dtype={"post_id": str}, parse_dates=["fecha_publicacion_utc"])
    faltan = {"username", "fecha_publicacion_utc", "log_likes", "formato", "franja"} - set(df.columns)
    if faltan:
        raise ErrorFatal(f"Al CSV le faltan columnas: {', '.join(sorted(faltan))}")
    return df.sort_values(["fecha_publicacion_utc", "post_id"]).reset_index(drop=True)


def nivel_previo(df: pd.DataFrame, ventana: int = 20) -> pd.Series:
    """Promedio de log_likes de los `ventana` posts ANTERIORES de la misma cuenta. NaN en su primer post."""
    orden = df.sort_values(["username", "fecha_publicacion_utc", "post_id"])
    previo = orden.groupby("username")["log_likes"].transform(
        lambda s: s.shift(1).rolling(ventana, min_periods=1).mean())
    return previo.reindex(df.index)


# Columnas del CSV que necesita el modelo (se validan antes de entrenar)
COLUMNAS_MODELO = ["post_id", "username", "fecha_publicacion_utc", "likes", "log_likes", "log_seguidores", "formato",
                   "tiene_llamado_accion", "menciona_precio_promo", "tiene_pregunta", "es_fin_de_semana", "franja",
                   "largo_texto_propio", "hashtags_propios", "emojis_propios", "posts_ultimos_7_dias"]


def matriz_features(df: pd.DataFrame) -> pd.DataFrame:
    a_int = lambda s: s.astype(bool).astype(int)
    return pd.DataFrame({
        "log_seguidores": df["log_seguidores"].astype(float),
        "es_reel": (df["formato"] == "reel").astype(int),
        "es_carrusel": (df["formato"] == "carrusel").astype(int),     # referencia: imagen (y video del feed)
        "tiene_llamado_accion": a_int(df["tiene_llamado_accion"]),
        "menciona_precio_promo": a_int(df["menciona_precio_promo"]),
        "tiene_pregunta": a_int(df["tiene_pregunta"]),
        "es_fin_de_semana": a_int(df["es_fin_de_semana"]),
        "franja_tarde": (df["franja"] == "tarde").astype(int),        # referencia: mañana
        "franja_noche": (df["franja"] == "noche").astype(int),
        "franja_madrugada": (df["franja"] == "madrugada").astype(int),
        "log_largo_texto": np.log1p(df["largo_texto_propio"].astype(float)),
        "log_hashtags": np.log1p(df["hashtags_propios"].astype(float)),
        "log_emojis": np.log1p(df["emojis_propios"].astype(float)),
        "posts_ultimos_7_dias": df["posts_ultimos_7_dias"].astype(float),
    }, index=df.index)


def marcar_virales(df: pd.DataFrame) -> pd.Series:
    """Posts muy por encima de lo normal en su cuenta (z robusto > 3,5). Solo para reportar el error sin ellos."""
    def z(s):
        mad = 1.4826 * (s - s.median()).abs().median()
        return (s - s.median()) / mad if mad > 0 else s * 0
    return df.groupby("username")["log_likes"].transform(z) > Z_VIRAL


# --------------------------------------------------------------------------------------------
# Modelos y métricas
# --------------------------------------------------------------------------------------------

def catalogo_modelos() -> dict[str, Callable[[], Pipeline]]:
    escalar = lambda modelo: Pipeline([("escalar", StandardScaler()), ("modelo", modelo)])
    return {
        "Regresión lineal": lambda: escalar(LinearRegression()),
        "Regresión lineal (descenso de gradiente propio)": lambda: escalar(RegresionLinealGD()),
        "Ridge": lambda: escalar(RidgeCV(alphas=np.logspace(-2, 4, 30))),
        "Huber (lineal robusta)": lambda: escalar(HuberRegressor(max_iter=5000)),
        "Random Forest": lambda: escalar(RandomForestRegressor(n_estimators=400, min_samples_leaf=15,
                                                               random_state=SEMILLA, n_jobs=-1)),
    }


def metricas(y: np.ndarray, pred: np.ndarray, usuarios: pd.Series, virales: pd.Series) -> dict:
    error = np.abs(np.asarray(y) - np.asarray(pred))
    normales = ~np.asarray(virales)
    ss_res = ((np.asarray(y) - np.asarray(pred)) ** 2).sum()
    ss_tot = ((np.asarray(y) - np.mean(y)) ** 2).sum()
    mae = error.mean()
    return {
        "posts": len(error),
        "MAE": mae,
        "error_tipico_x": float(np.exp(mae)),           # se equivoca en promedio por este factor (×)
        "MAE_sin_virales": error[normales].mean(),
        "MedAE": float(np.median(error)),
        "MAE_por_cuenta": pd.Series(error, index=usuarios.index).groupby(usuarios.values).mean().mean(),
        "R2": 1 - ss_res / ss_tot,
    }


def ajustar(fabrica: Callable[[], Pipeline], X: pd.DataFrame, y: np.ndarray) -> Pipeline:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConvergenceWarning)
        return fabrica().fit(X, y)


# --------------------------------------------------------------------------------------------
# Escenario A: barbería conocida (validación temporal)
# --------------------------------------------------------------------------------------------

def cortes_temporales(fechas: pd.Series, n_cortes: int = 4, desde: float = 0.5) -> list[tuple]:
    """Ventanas de prueba consecutivas desde el cuantil `desde`; se entrena siempre con todo lo anterior."""
    limites = list(fechas.quantile(np.linspace(desde, 1, n_cortes + 1)))
    limites[-1] = fechas.max() + pd.Timedelta(seconds=1)
    return list(zip(limites[:-1], limites[1:]))


def evaluar_barberia_conocida(df: pd.DataFrame, ventana: int = 20, n_cortes: int = 4) -> tuple[pd.DataFrame, pd.DataFrame]:
    y = df["log_likes"].to_numpy()
    X_post = matriz_features(df)
    previo = nivel_previo(df, ventana)
    nombres_base = ["Mediana global", "Promedio histórico de la cuenta", f"Nivel reciente ({ventana} posts)"]
    catalogo = catalogo_modelos()
    nombres = nombres_base + [f"{m} + nivel" for m in catalogo] + ["Regresión lineal sin nivel"]
    pred = {n: np.full(len(df), np.nan) for n in nombres}

    for inicio, fin in cortes_temporales(df["fecha_publicacion_utc"], n_cortes):
        tr = (df["fecha_publicacion_utc"] < inicio).to_numpy()
        te = ((df["fecha_publicacion_utc"] >= inicio) & (df["fecha_publicacion_utc"] < fin)).to_numpy()
        if not tr.any() or not te.any():
            continue
        media_global = y[tr].mean()
        media_cuenta = df.loc[tr].groupby("username")["log_likes"].mean()
        nivel = previo.fillna(media_global)                          # primer post de una cuenta: promedio global
        X = X_post.assign(**{FEATURE_NIVEL: nivel})

        pred["Mediana global"][te] = np.median(y[tr])
        pred["Promedio histórico de la cuenta"][te] = df.loc[te, "username"].map(media_cuenta).fillna(media_global)
        pred[f"Nivel reciente ({ventana} posts)"][te] = nivel[te]
        for nombre, fabrica in catalogo.items():
            pred[f"{nombre} + nivel"][te] = ajustar(fabrica, X[tr], y[tr]).predict(X[te])
        pred["Regresión lineal sin nivel"][te] = ajustar(catalogo["Regresión lineal"], X_post[tr], y[tr]).predict(X_post[te])

    evaluado = ~np.isnan(pred["Mediana global"])
    virales = marcar_virales(df)
    tabla = pd.DataFrame({n: metricas(y[evaluado], p[evaluado], df.loc[evaluado, "username"], virales[evaluado])
                          for n, p in pred.items()}).T
    tabla["tipo"] = ["referencia" if n in nombres_base else "modelo" for n in tabla.index]
    predicciones = df.loc[evaluado, ["post_id", "username", "fecha_publicacion_utc", "formato", "likes", "log_likes"]].copy()
    predicciones["es_viral"] = virales[evaluado].to_numpy()
    for n, p in pred.items():
        predicciones[n] = p[evaluado]
    return tabla.sort_values("MAE"), predicciones


# --------------------------------------------------------------------------------------------
# Escenario B: barbería nueva (GroupKFold por cuenta)
# --------------------------------------------------------------------------------------------

def evaluar_barberia_nueva(df: pd.DataFrame, n_folds: int = 5) -> pd.DataFrame:
    y = df["log_likes"].to_numpy()
    X = matriz_features(df)
    grupos = df["username"].to_numpy()
    catalogo = {k: v for k, v in catalogo_modelos().items() if "gradiente" not in k}
    nombres = ["Mediana global", "Solo seguidores"] + list(catalogo)
    pred = {n: np.full(len(df), np.nan) for n in nombres}
    for tr, te in GroupKFold(n_splits=min(n_folds, len(set(grupos)))).split(X, y, grupos):
        pred["Mediana global"][te] = np.median(y[tr])
        pred["Solo seguidores"][te] = LinearRegression().fit(X.iloc[tr][["log_seguidores"]], y[tr]).predict(
            X.iloc[te][["log_seguidores"]])
        for nombre, fabrica in catalogo.items():
            pred[nombre][te] = ajustar(fabrica, X.iloc[tr], y[tr]).predict(X.iloc[te])
    virales = marcar_virales(df)
    tabla = pd.DataFrame({n: metricas(y, p, df["username"], virales) for n, p in pred.items()}).T
    tabla["tipo"] = ["referencia" if n in ("Mediana global", "Solo seguidores") else "modelo" for n in tabla.index]
    return tabla.sort_values("MAE")


# --------------------------------------------------------------------------------------------
# Modelo final
# --------------------------------------------------------------------------------------------

def elegir_modelo(tabla_a: pd.DataFrame) -> str:
    """El modelo (no referencia) con menor MAE en el escenario A. Devuelve su nombre en el catálogo."""
    modelos = tabla_a[(tabla_a["tipo"] == "modelo") & tabla_a.index.str.endswith(" + nivel")]
    return modelos["MAE"].astype(float).idxmin().removesuffix(" + nivel")


def entrenar_final(df: pd.DataFrame, nombre: str, ventana: int = 20) -> tuple[Pipeline, list[str], float]:
    y = df["log_likes"].to_numpy()
    media_global = float(y.mean())
    X = matriz_features(df).assign(**{FEATURE_NIVEL: nivel_previo(df, ventana).fillna(media_global)})
    return ajustar(catalogo_modelos()[nombre], X, y), list(X.columns), media_global


def coeficientes(pipeline: Pipeline, columnas: list[str]) -> pd.DataFrame | None:
    """Coeficientes en escala original y su efecto en % sobre los likes. None si el modelo no es lineal."""
    modelo, escalar = pipeline.named_steps["modelo"], pipeline.named_steps["escalar"]
    if not hasattr(modelo, "coef_"):
        return None
    coef = np.asarray(modelo.coef_) / escalar.scale_
    filas = []
    for col, c_est, c in zip(columnas, modelo.coef_, coef):
        tipo, lectura = TIPO_FEATURE[col]
        cambio = {"log": np.log(2), "binaria": 1.0, "conteo": 1.0, "nivel": 0.1}[tipo]
        filas.append({"variable": col, "tipo": tipo, "lectura": lectura, "coef_estandarizado": c_est,
                      "coef_original": c, "efecto_%": (np.exp(c * cambio) - 1) * 100})
    return pd.DataFrame(filas).sort_values("coef_estandarizado", key=abs, ascending=False).reset_index(drop=True)


# --------------------------------------------------------------------------------------------
# Ejecución
# --------------------------------------------------------------------------------------------

def configurar_log() -> Path:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    carpeta = RAIZ / "logs"
    carpeta.mkdir(exist_ok=True)
    archivo = carpeta / f"modelo_{datetime.now():%Y%m%d_%H%M%S}.log"
    formato = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    consola = logging.StreamHandler(sys.stdout)
    consola.setFormatter(formato)
    en_archivo = logging.FileHandler(archivo, encoding="utf-8")
    en_archivo.setFormatter(formato)
    log.handlers[:] = [consola, en_archivo]
    log.setLevel(logging.INFO)
    return archivo


def tabla_texto(tabla: pd.DataFrame) -> str:
    vista = tabla[["MAE", "error_tipico_x", "MAE_sin_virales", "MedAE", "MAE_por_cuenta", "R2"]].astype(float).round(3)
    vista.columns = ["MAE", "error ×", "MAE sin virales", "MedAE", "MAE x cuenta", "R²"]
    return vista.to_string()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Entrena y compara modelos para predecir los likes de un post.")
    p.add_argument("--csv", default=str(RAIZ / "data" / "posts_features.csv"))
    p.add_argument("--ventana", type=int, default=20, help="Posts anteriores para el nivel reciente (por defecto 20)")
    p.add_argument("--cortes", type=int, default=4, help="Cortes de la validación temporal (por defecto 4)")
    p.add_argument("--folds-cuenta", type=int, default=5, help="Folds de la validación por cuenta (por defecto 5)")
    p.add_argument("--aceptar-modelo", action="store_true",
                   help="Guardar el modelo final aunque no pase el control de calidad (después de revisarlo)")
    args = p.parse_args(argv)
    archivo_log = configurar_log()

    try:
        df = cargar(Path(args.csv))
    except ErrorFatal as e:
        log.error("%s", e)
        return 2

    # Control de calidad de los datos
    validacion = validar_posts(df, columnas_requeridas=COLUMNAS_MODELO)
    ruta_reporte = guardar_reporte(validacion, "modelo", RAIZ / "reportes" / "calidad")
    log.info("%s", validacion.resumen())
    for linea in validacion.lineas_problemas():
        log.warning("  %s", linea)
    if not validacion.ok:
        log.error("Los datos no pasaron el control de calidad: no se entrenó nada. Detalle en %s", ruta_reporte)
        return 3
    log.info("%d posts de %d cuentas (%s a %s)", len(df), df["username"].nunique(),
             df["fecha_publicacion_utc"].min().date(), df["fecha_publicacion_utc"].max().date())

    log.info("Escenario A — barbería conocida (validación temporal, %d cortes)…", args.cortes)
    tabla_a, predicciones = evaluar_barberia_conocida(df, args.ventana, args.cortes)
    log.info("Posts evaluados: %d\n%s", int(tabla_a["posts"].iloc[0]), tabla_texto(tabla_a))

    log.info("Escenario B — barbería nueva (GroupKFold por cuenta, %d folds)…", args.folds_cuenta)
    tabla_b = evaluar_barberia_nueva(df, args.folds_cuenta)
    log.info("\n%s", tabla_texto(tabla_b))

    nombre = elegir_modelo(tabla_a)
    final, columnas, media_global = entrenar_final(df, nombre, args.ventana)
    fila = tabla_a.loc[f"{nombre} + nivel"]
    referencia = tabla_a.loc[f"Nivel reciente ({args.ventana} posts)"]
    mejora = (1 - float(fila["MAE"]) / float(referencia["MAE"])) * 100
    log.info("Modelo final: %s + nivel | MAE %.3f (error típico ×%.2f) | %.1f%% mejor que solo el nivel reciente",
             nombre, fila["MAE"], fila["error_tipico_x"], mejora)

    carpeta_datos, carpeta_modelos = RAIZ / "data", RAIZ / "modelos"
    carpeta_modelos.mkdir(exist_ok=True)
    resultados = pd.concat({"A_barberia_conocida": tabla_a, "B_barberia_nueva": tabla_b}, names=["escenario", "modelo"])
    resultados.to_csv(carpeta_datos / "resultados_modelos.csv", encoding="utf-8-sig")
    predicciones.to_csv(carpeta_datos / "predicciones_validacion.csv", index=False, encoding="utf-8-sig")
    coefs = coeficientes(final, columnas)
    meta = {
        "modelo": nombre, "features": columnas, "target": "log_likes = ln(1 + likes)",
        "nivel_reciente": f"promedio de log_likes de los {args.ventana} posts anteriores de la cuenta; "
                          f"si no hay, {media_global:.4f} (promedio global)",
        "ventana_nivel": args.ventana, "promedio_global_log_likes": media_global,
        "entrenado_con_posts": len(df), "cuentas": int(df["username"].nunique()),
        "metricas_validacion_temporal": {k: float(v) for k, v in fila.drop("tipo").items()},
        "metricas_referencia_nivel_reciente": {k: float(v) for k, v in referencia.drop("tipo").items()},
        "fecha_entrenamiento": datetime.now().isoformat(timespec="seconds"),
        "versiones": {"python": sys.version.split()[0], "scikit-learn": sklearn.__version__,
                      "pandas": pd.__version__, "numpy": np.__version__},
    }

    # Control de calidad del modelo: solo reemplaza al guardado si pasa
    ruta_meta = carpeta_modelos / "modelo_final.json"
    anterior = json.loads(ruta_meta.read_text(encoding="utf-8")) if ruta_meta.exists() else None
    X_final = matriz_features(df).assign(**{FEATURE_NIVEL: nivel_previo(df, args.ventana).fillna(media_global)})
    control = controlar_modelo(meta["metricas_validacion_temporal"], meta["metricas_referencia_nivel_reciente"],
                               final.predict(X_final), anterior,
                               coefs["coef_original"].to_numpy() if coefs is not None else None)
    for c in control.chequeos:
        log.info("  Control %-22s %s  %s", c.nombre, "OK   " if c.aprobado else "FALLA", c.detalle)
    meta["control_calidad"] = control.como_lista()
    if not control.aprobado and not args.aceptar_modelo:
        (carpeta_modelos / "candidato_rechazado.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                                                   encoding="utf-8")
        log.error("El modelo nuevo no pasó el control de calidad (%s): se mantiene el modelo guardado. "
                  "Revisa modelos/candidato_rechazado.json; si el cambio es esperable (por ejemplo, datos nuevos), "
                  "vuelve a correr con --aceptar-modelo.", ", ".join(c.nombre for c in control.fallidos))
        return 4
    if not control.aprobado:
        log.warning("El modelo no pasó el control de calidad, pero se guarda porque se usó --aceptar-modelo.")
        meta["aceptado_manualmente"] = True

    if coefs is not None:
        coefs.to_csv(carpeta_datos / "coeficientes_modelo.csv", index=False, encoding="utf-8-sig")
    joblib.dump(final, carpeta_modelos / "modelo_final.joblib")
    ruta_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (carpeta_modelos / "candidato_rechazado.json").unlink(missing_ok=True)
    log.info("Guardado: data/resultados_modelos.csv, data/predicciones_validacion.csv, data/coeficientes_modelo.csv, "
             "modelos/modelo_final.joblib y modelos/modelo_final.json")
    log.info("Log completo en %s", archivo_log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
