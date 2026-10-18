"""
analisis-barberias-peluquerias — Fase 5: datos para el dashboard de Power BI

Junta los resultados de las fases 2 a 5 en tablas listas para importar en Power BI (powerbi/datos/).
Por defecto las cuentas salen como "Barbería 01..N" (mismo código que en los notebooks) y sin textos de los posts,
para poder publicar capturas del dashboard sin nombrar a nadie.

Tablas:
  posts.csv          un post por fila: cuenta, fecha, formato, franja, variables del texto, likes, si fue viral
  cuentas.csv        una cuenta por fila: seguidores, posts, likes típicos, % reels, error del modelo
  embudo.csv         cuántas cuentas y posts quedaron en cada paso de la limpieza (sin nombres)
  efectos.csv        efecto de cada variable del modelo con su intervalo y recomendación (Fase 5)
  efectos_simples.csv comparaciones dentro de cada cuenta de la Fase 3, con intervalo
  modelos.csv        error de cada modelo en los dos escenarios de validación (Fase 4)
  predicciones.csv   likes reales vs predichos en la validación temporal (Fase 4)

Uso:
    python preparar_dashboard.py                 # anonimizado (recomendado)
    python preparar_dashboard.py --con-nombres   # con los usernames reales, solo para uso personal
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import entrenar_modelo as em
from anonimizar import mapa_cuentas
from interpretar_modelo import NOMBRES

RAIZ = Path(__file__).resolve().parent
DATA = RAIZ / "data"
SALIDA = RAIZ / "powerbi" / "datos"

FORMATOS = {"reel": "Reel", "carrusel": "Carrusel", "imagen": "Imagen", "video_feed": "Video"}
FRANJAS = {"madrugada": "Madrugada (0-6 h)", "manana": "Mañana (6-12 h)", "tarde": "Tarde (12-18 h)",
           "noche": "Noche (18-24 h)"}
ORDEN_FRANJA = {"madrugada": 1, "manana": 2, "tarde": 3, "noche": 4}
MODELO_PRINCIPAL = "Huber (lineal robusta) + nivel"
REFERENCIA = "Nivel reciente (20 posts)"


class FaltaArchivo(Exception):
    pass


def leer(nombre: str, obligatorio: bool = True) -> pd.DataFrame | None:
    ruta = DATA / nombre
    if not ruta.exists():
        if obligatorio:
            raise FaltaArchivo(f"Falta {ruta.relative_to(RAIZ)}")
        print(f"  (aviso) no existe {ruta.relative_to(RAIZ)}: se omite esa tabla")
        return None
    return pd.read_csv(ruta, encoding="utf-8-sig")


def tabla_posts(posts: pd.DataFrame, cuenta: dict[str, str]) -> pd.DataFrame:
    df = posts.copy()
    df["fecha_publicacion_utc"] = pd.to_datetime(df["fecha_publicacion_utc"])
    df = df.sort_values(["fecha_publicacion_utc", "post_id"]).reset_index(drop=True)
    local = pd.to_datetime(df["fecha_local"])
    a_int = lambda c: df[c].astype(str).str.lower().isin(["true", "1"]).astype(int)
    return pd.DataFrame({
        "id_post": np.arange(1, len(df) + 1),
        "cuenta": df["username"].map(cuenta),
        "fecha": local.dt.strftime("%Y-%m-%d"),
        "fecha_hora_local": local.dt.strftime("%Y-%m-%d %H:%M:%S"),
        "mes": local.dt.strftime("%Y-%m"),
        "dia_semana": df["nombre_dia"].str.capitalize(),
        "orden_dia": df["dia_semana"].astype(int) + 1,          # 1 = lunes, para ordenar en Power BI
        "fin_de_semana": a_int("es_fin_de_semana"),
        "hora_local": df["hora_local"].astype(int),
        "franja": df["franja"].map(FRANJAS),
        "orden_franja": df["franja"].map(ORDEN_FRANJA),
        "formato": df["formato"].map(FORMATOS).fillna(df["formato"]),
        "largo_texto": df["largo_texto_propio"].astype(int),
        "hashtags": df["hashtags_propios"].astype(int),
        "emojis": df["emojis_propios"].astype(int),
        "tiene_pregunta": a_int("tiene_pregunta"),
        "llamado_accion": a_int("tiene_llamado_accion"),
        "precio_promo": a_int("menciona_precio_promo"),
        "posts_7_dias": df["posts_ultimos_7_dias"].astype(int),
        "seguidores": df["seguidores"].astype(int),
        "likes": df["likes"].astype(int),
        "comentarios": df["comentarios"].astype(int),
        "log_likes": df["log_likes"].round(6),
        "engagement_pct": (df["engagement_rate"] * 100).round(4),
        "viral": em.marcar_virales(df).astype(int),
    })


def tabla_cuentas(posts: pd.DataFrame, pred: pd.DataFrame | None, cuenta: dict[str, str]) -> pd.DataFrame:
    g = posts.groupby("username")
    t = pd.DataFrame({
        "seguidores": g["seguidores"].max(),
        "posts": g.size(),
        "likes_mediana": g["likes"].median(),
        "likes_promedio": g["likes"].mean().round(1),
        "likes_total": g["likes"].sum(),
        "pct_reels": (g["formato"].apply(lambda s: (s == "reel").mean()) * 100).round(1),
        "engagement_mediano_pct": (g["engagement_rate"].median() * 100).round(3),
        "primer_post": pd.to_datetime(g["fecha_local"].min()).dt.strftime("%Y-%m-%d"),
        "ultimo_post": pd.to_datetime(g["fecha_local"].max()).dt.strftime("%Y-%m-%d"),
    })
    if pred is not None:
        e = pred.assign(err_modelo=(pred[MODELO_PRINCIPAL] - pred["log_likes"]).abs(),
                        err_ref=(pred[REFERENCIA] - pred["log_likes"]).abs()).groupby("username")
        t["posts_validacion"] = e.size()
        t["error_tipico_modelo_x"] = np.exp(e["err_modelo"].mean()).round(3)
        t["error_tipico_referencia_x"] = np.exp(e["err_ref"].mean()).round(3)
        t["modelo_mejora_referencia"] = (e["err_modelo"].mean() < e["err_ref"].mean()).astype("Int64")
    t = t.reset_index()
    t.insert(0, "cuenta", t.pop("username").map(cuenta))
    return t.sort_values("cuenta").reset_index(drop=True)


def tabla_embudo(resumen: pd.DataFrame, lista: pd.DataFrame | None = None) -> pd.DataFrame:
    incl = resumen["incluida"].astype(str).str.lower() == "true"
    pasos = []
    if lista is not None:
        con_usuario = lista["username"].fillna("").astype(str).str.strip().str.lstrip("@").str.lower()
        pasos += [("Cuentas", "Barberías en la lista", len(lista)),
                  ("Cuentas", "Con Instagram identificado", int(con_usuario[con_usuario != ""].nunique()))]
    pasos += [
        ("Cuentas", "Cuentas extraídas", len(resumen)),
        ("Cuentas", "Con posts en los últimos 12 meses", int((resumen["posts_en_ventana"] > 0).sum())),
        ("Cuentas", "Incluidas en el análisis", int(incl.sum())),
        ("Posts", "Posts extraídos", int(resumen["posts_extraidos"].sum())),
        ("Posts", "En la ventana de 12 meses", int(resumen["posts_en_ventana"].sum())),
        ("Posts", "Válidos (sin sorteos, likes ocultos ni posts demasiado recientes)", int(resumen["posts_validos"].sum())),
        ("Posts", "Usados en el modelo", int(resumen.loc[incl, "posts_validos"].sum())),
    ]
    return pd.DataFrame([{"etapa": e, "orden": i, "paso": p, "cantidad": n} for i, (e, p, n) in enumerate(pasos, 1)])


def tabla_efectos(interp: pd.DataFrame) -> pd.DataFrame:
    t = interp.copy()
    t.insert(1, "nombre", t["variable"].map(NOMBRES).fillna(t["variable"]))
    t["orden"] = np.arange(1, len(t) + 1)
    for c in ("coef_original", "efecto_%", "efecto_bajo_%", "efecto_alto_%", "signo_estable_%"):
        t[c] = t[c].round(3)
    return t.rename(columns={"efecto_%": "efecto_pct", "efecto_bajo_%": "efecto_bajo_pct",
                             "efecto_alto_%": "efecto_alto_pct", "signo_estable_%": "signo_estable_pct"})


def tabla_predicciones(pred: pd.DataFrame, posts_bi: pd.DataFrame, posts: pd.DataFrame,
                       cuenta: dict[str, str]) -> pd.DataFrame:
    ids = dict(zip(posts.sort_values(["fecha_publicacion_utc", "post_id"])["post_id"].astype(str), posts_bi["id_post"]))
    p = pred.copy()
    real, modelo, ref = p["log_likes"], p[MODELO_PRINCIPAL], p[REFERENCIA]
    return pd.DataFrame({
        "id_post": p["post_id"].astype(str).map(ids),
        "cuenta": p["username"].map(cuenta),
        "fecha": pd.to_datetime(p["fecha_publicacion_utc"]).dt.strftime("%Y-%m-%d"),
        "formato": p["formato"].map(FORMATOS).fillna(p["formato"]),
        "viral": p["es_viral"].astype(str).str.lower().isin(["true", "1"]).astype(int),
        "likes": p["likes"].astype(int),
        "likes_predichos_modelo": np.expm1(modelo).round(1),
        "likes_predichos_referencia": np.expm1(ref).round(1),
        "error_log_modelo": (modelo - real).abs().round(6),
        "error_log_referencia": (ref - real).abs().round(6),
        "veces_de_error_modelo": np.exp((modelo - real).abs()).round(3),
    })


def tabla_modelos(res: pd.DataFrame) -> pd.DataFrame:
    t = res.copy()
    t["escenario"] = t["escenario"].map({"A_barberia_conocida": "A. Barbería conocida (próximos posts)",
                                         "B_barberia_nueva": "B. Barbería nueva (sin historial)"})
    t["es_modelo_final"] = (t["modelo"] == MODELO_PRINCIPAL).astype(int)
    return t.round(4)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Exporta las tablas del dashboard de Power BI a powerbi/datos/.")
    p.add_argument("--con-nombres", action="store_true",
                   help="Mantener los usernames reales (no publicar capturas ni archivos hechos con esta opción)")
    args = p.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    try:
        posts = leer("posts_features.csv")
        resumen = leer("cuentas_resumen.csv")
    except FaltaArchivo as e:
        print(f"ERROR: {e}. Corre antes preparar_features.py (Fase 2).")
        return 2
    pred = leer("predicciones_validacion.csv", obligatorio=False)
    res = leer("resultados_modelos.csv", obligatorio=False)
    interp = leer("interpretacion_coeficientes.csv", obligatorio=False)
    simples = leer("efectos_dentro_de_cuentas.csv", obligatorio=False)

    cuenta = ({u: u for u in posts["username"].unique()} if args.con_nombres else mapa_cuentas(posts["username"]))
    posts_bi = tabla_posts(posts, cuenta)
    tablas = {
        "posts": posts_bi,
        "cuentas": tabla_cuentas(posts, pred, cuenta),
        "embudo": tabla_embudo(resumen, leer("barberias_santiago.csv", obligatorio=False)),
    }
    if interp is not None:
        tablas["efectos"] = tabla_efectos(interp)
    if simples is not None:
        tablas["efectos_simples"] = simples.rename(columns=lambda c: c.replace("_%", "_pct").replace("señal", "senal"))
    if res is not None:
        tablas["modelos"] = tabla_modelos(res)
    if pred is not None:
        tablas["predicciones"] = tabla_predicciones(pred, posts_bi, posts, cuenta)

    SALIDA.mkdir(parents=True, exist_ok=True)
    for nombre, t in tablas.items():
        t.to_csv(SALIDA / f"{nombre}.csv", index=False, encoding="utf-8-sig")
        print(f"  powerbi/datos/{nombre}.csv  ({len(t)} filas)")
    print("Listo. Modo:", "CON NOMBRES REALES (solo uso personal)" if args.con_nombres else "anonimizado")
    print("Sigue los pasos de powerbi/GUIA_DASHBOARD.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
