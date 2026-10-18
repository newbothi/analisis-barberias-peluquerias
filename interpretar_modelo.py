"""
analisis-barberias-peluquerias — Fase 5: interpretación

Traduce el modelo final a efectos en % sobre los likes, con intervalos de confianza, y a recomendaciones.

- Coeficientes del modelo final con IC 90% por bootstrap de cuentas: se remuestrean cuentas completas (no posts,
  porque los posts de una misma cuenta no son independientes) y se reentrena el modelo cada vez.
- Efectos "dentro de cada cuenta" de la Fase 3 (comparaciones simples, sin modelo), con el mismo bootstrap.
- Una variable solo genera recomendación si su intervalo no cruza el 0.

Lee data/posts_features.csv y modelos/modelo_final.json (Fase 4). Escribe:
  data/interpretacion_coeficientes.csv   efecto de cada variable del modelo, IC y recomendación
  data/efectos_dentro_de_cuentas.csv     comparaciones de la Fase 3 con IC

Uso:
    python interpretar_modelo.py
    python interpretar_modelo.py --repeticiones 1000
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import warnings
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.exceptions import ConvergenceWarning

import entrenar_modelo as em

RAIZ = Path(__file__).resolve().parent
SEMILLA = 42
NIVEL_CONFIANZA = 0.90

# Qué recomendar con cada variable si su efecto es claro: (accionable, si aumenta los likes, si los reduce).
# "accionable" = la barbería lo decide al publicar. Las otras dos son controles: se describen, no se recomiendan.
GUIA = {
    "es_reel": (True, "Priorizar reels por sobre imágenes sueltas.",
                "Los reels rinden menos que las imágenes: no conviene priorizarlos."),
    "es_carrusel": (True, "Si no es reel, preferir un carrusel antes que una imagen sola.",
                    "Los carruseles rinden menos que las imágenes sueltas."),
    "tiene_llamado_accion": (True, "Los llamados a la acción suman likes.",
                             "Los posts promocionales (agenda, reserva, WhatsApp) reciben menos likes. No dejar de "
                             "publicarlos, pero medir su éxito por reservas y no por likes, y alternarlos con contenido."),
    "menciona_precio_promo": (True, "Mencionar precios o promociones suma likes.",
                              "Los posts con precios o promociones reciben menos likes. Igual que el llamado a la acción: "
                              "su objetivo es vender, no sumar likes. Ojo: hay pocos posts de este tipo."),
    "tiene_pregunta": (True, "Hacer preguntas en el texto.", "Evitar las preguntas en el texto."),
    "es_fin_de_semana": (True, "Publicar en fin de semana.", "Publicar de lunes a viernes."),
    "franja_tarde": (True, "Publicar en la tarde (12-18 h) antes que en la mañana.",
                     "Publicar en la mañana antes que en la tarde."),
    "franja_noche": (True, "Publicar en la noche (18-24 h) antes que en la mañana.",
                     "Publicar en la mañana antes que en la noche."),
    "franja_madrugada": (True, "Publicar de madrugada.", "Evitar publicar de madrugada."),
    "log_largo_texto": (True, "Escribir un texto propio más desarrollado (además de la firma fija de la cuenta).",
                        "Preferir textos cortos."),
    "log_hashtags": (True, "Usar más hashtags propios del post.", "Usar menos hashtags."),
    "log_emojis": (True, "Usar emojis en el texto.", "Usar menos emojis."),
    "posts_ultimos_7_dias": (True, "Publicar con más frecuencia.",
                             "Publicar seguido le quita likes a cada post: espaciar las publicaciones."),
    "log_seguidores": (False, "Las cuentas más grandes tienen más likes, pero menos que proporcional al tamaño.",
                       "Las cuentas más grandes tienen menos likes por post."),
    "nivel_reciente": (False, "El rendimiento reciente de la cuenta es lo que más predice el próximo post.",
                       "El rendimiento reciente de la cuenta va en contra del próximo post."),
}

# Nombre corto de cada variable para gráficos y dashboard
NOMBRES = {
    "es_reel": "Reel (vs imagen)", "es_carrusel": "Carrusel (vs imagen)",
    "tiene_llamado_accion": "Llamado a la acción", "menciona_precio_promo": "Precio o promoción",
    "tiene_pregunta": "Pregunta en el texto", "es_fin_de_semana": "Fin de semana",
    "franja_tarde": "Tarde (vs mañana)", "franja_noche": "Noche (vs mañana)", "franja_madrugada": "Madrugada (vs mañana)",
    "log_largo_texto": "Texto el doble de largo", "log_hashtags": "El doble de hashtags",
    "log_emojis": "El doble de emojis", "posts_ultimos_7_dias": "Un post más en la semana",
    "log_seguidores": "El doble de seguidores", "nivel_reciente": "Cuenta un 10% más arriba últimamente",
}

log = logging.getLogger("interpretacion")


def _sin_avisos():
    warnings.simplefilter("ignore", ConvergenceWarning)


def remuestrear_cuentas(df: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """Muestra bootstrap de cuentas completas. Una cuenta elegida dos veces entra como dos cuentas distintas."""
    cuentas = df["username"].unique()
    elegidas = rng.choice(cuentas, size=len(cuentas), replace=True)
    partes = [df[df["username"] == c].assign(username=f"{c}#{k}") for k, c in enumerate(elegidas)]
    return (pd.concat(partes, ignore_index=True)
              .sort_values(["fecha_publicacion_utc", "post_id"]).reset_index(drop=True))


def bootstrap_coeficientes(df: pd.DataFrame, nombre_modelo: str, repeticiones: int = 500,
                           semilla: int = SEMILLA) -> pd.DataFrame:
    """Coeficientes (escala original) del modelo reentrenado en cada muestra bootstrap. Una fila por repetición."""
    _sin_avisos()
    rng = np.random.default_rng(semilla)
    filas = []
    for _ in range(repeticiones):
        muestra = remuestrear_cuentas(df, rng)
        modelo, columnas, _ = em.entrenar_final(muestra, nombre_modelo)
        coefs = em.coeficientes(modelo, columnas)
        if coefs is None:
            raise ValueError(f"{nombre_modelo} no es un modelo lineal: no tiene coeficientes que interpretar")
        filas.append(coefs.set_index("variable")["coef_original"])
    return pd.DataFrame(filas).reset_index(drop=True)


def resumir_coeficientes(df: pd.DataFrame, nombre_modelo: str, boot: pd.DataFrame) -> pd.DataFrame:
    _sin_avisos()
    modelo, columnas, _ = em.entrenar_final(df, nombre_modelo)
    base = em.coeficientes(modelo, columnas).set_index("variable")
    boot = boot[base.index]
    alfa = (1 - NIVEL_CONFIANZA) / 2
    bajo, alto = boot.quantile(alfa), boot.quantile(1 - alfa)
    cambio = base["tipo"].map({"log": np.log(2), "binaria": 1.0, "conteo": 1.0, "nivel": 0.1})
    a_pct = lambda c: (np.exp(c * cambio) - 1) * 100

    tabla = pd.DataFrame({
        "variable": base.index,
        "lectura": base["lectura"],
        "tipo": base["tipo"],
        "coef_original": base["coef_original"],
        "efecto_%": base["efecto_%"],
        "efecto_bajo_%": a_pct(bajo),
        "efecto_alto_%": a_pct(alto),
        "signo_estable_%": (np.sign(boot) == np.sign(base["coef_original"])).mean() * 100,
    }).reset_index(drop=True)

    def conclusion(f):
        if f["efecto_bajo_%"] > 0:
            return "aumenta los likes"
        if f["efecto_alto_%"] < 0:
            return "reduce los likes"
        return "sin evidencia suficiente"

    def recomendacion(f):
        accionable, si_sube, si_baja = GUIA.get(f["variable"], (False, "", ""))
        if f["conclusion"] == "sin evidencia suficiente":
            return "No hay evidencia para decidir por esto." if accionable else ""
        return si_sube if f["conclusion"] == "aumenta los likes" else si_baja

    tabla["conclusion"] = tabla.apply(conclusion, axis=1)
    tabla["magnitud"] = np.where(tabla["efecto_%"].abs() >= 15, "grande",
                                 np.where(tabla["efecto_%"].abs() >= 5, "moderada", "chica"))
    tabla["accionable"] = tabla["variable"].map(lambda v: GUIA.get(v, (False,))[0])
    tabla["recomendacion"] = tabla.apply(recomendacion, axis=1)
    orden = {"aumenta los likes": 0, "reduce los likes": 1, "sin evidencia suficiente": 2}
    return (tabla.assign(_o=tabla["conclusion"].map(orden), _m=-tabla["efecto_%"].abs())
                 .sort_values(["_o", "_m"]).drop(columns=["_o", "_m"]).reset_index(drop=True))


def efectos_dentro_de_cuentas(df: pd.DataFrame, n_boot: int = 4000, semilla: int = SEMILLA,
                              min_posts: int = 3) -> pd.DataFrame:
    """Comparaciones de la Fase 3: diferencia de log_likes promedio entre dos grupos, dentro de cada cuenta."""
    rng = np.random.default_rng(semilla)
    mediana_largo = df.groupby("username")["largo_texto_propio"].transform("median")
    a_bool = lambda s: s.astype(bool)
    comparaciones = {
        "Reel vs no reel": (df["formato"] == "reel", df["formato"] != "reel"),
        "Carrusel vs imagen": (df["formato"] == "carrusel", df["formato"] == "imagen"),
        "Con llamado a la acción vs sin": (a_bool(df["tiene_llamado_accion"]), ~a_bool(df["tiene_llamado_accion"])),
        "Con pregunta vs sin": (a_bool(df["tiene_pregunta"]), ~a_bool(df["tiene_pregunta"])),
        "Fin de semana vs lunes a viernes": (a_bool(df["es_fin_de_semana"]), ~a_bool(df["es_fin_de_semana"])),
        "Tarde vs mañana": (df["franja"] == "tarde", df["franja"] == "manana"),
        "Noche vs tarde": (df["franja"] == "noche", df["franja"] == "tarde"),
        "Texto más largo que lo habitual vs más corto": (df["largo_texto_propio"] > mediana_largo,
                                                         df["largo_texto_propio"] <= mediana_largo),
    }
    pct = lambda x: (np.exp(x) - 1) * 100
    alfa = (1 - NIVEL_CONFIANZA) / 2 * 100
    filas = []
    for nombre, (a, b) in comparaciones.items():
        difs = []
        for _, g in df.groupby("username"):
            va, vb = g.loc[a.loc[g.index], "log_likes"], g.loc[b.loc[g.index], "log_likes"]
            if len(va) >= min_posts and len(vb) >= min_posts:
                difs.append(va.mean() - vb.mean())
        difs = np.array(difs)
        if len(difs) == 0:
            continue
        medias = rng.choice(difs, size=(n_boot, len(difs)), replace=True).mean(axis=1)
        bajo, alto = np.percentile(medias, [alfa, 100 - alfa])
        filas.append({"comparacion": nombre, "cuentas": len(difs), "cuentas_a_favor": int((difs > 0).sum()),
                      "mediana_%": pct(np.median(difs)), "media_%": pct(difs.mean()),
                      "ic_bajo_%": pct(bajo), "ic_alto_%": pct(alto),
                      "señal": "clara" if bajo > 0 or alto < 0 else "no concluyente"})
    return pd.DataFrame(filas)


def nombre_modelo_final() -> str:
    meta = RAIZ / "modelos" / "modelo_final.json"
    if meta.exists():
        return json.loads(meta.read_text(encoding="utf-8"))["modelo"]
    log.warning("No existe modelos/modelo_final.json (Fase 4). Se usa Huber (lineal robusta).")
    return "Huber (lineal robusta)"


def configurar_log() -> Path:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    carpeta = RAIZ / "logs"
    carpeta.mkdir(exist_ok=True)
    archivo = carpeta / f"interpretacion_{datetime.now():%Y%m%d_%H%M%S}.log"
    formato = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    consola = logging.StreamHandler(sys.stdout)
    consola.setFormatter(formato)
    en_archivo = logging.FileHandler(archivo, encoding="utf-8")
    en_archivo.setFormatter(formato)
    log.handlers[:] = [consola, en_archivo]
    log.setLevel(logging.INFO)
    return archivo


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Interpreta el modelo final: efectos en % con IC y recomendaciones.")
    p.add_argument("--csv", default=str(RAIZ / "data" / "posts_features.csv"))
    p.add_argument("--repeticiones", type=int, default=500, help="Repeticiones del bootstrap (por defecto 500)")
    args = p.parse_args(argv)
    archivo_log = configurar_log()
    try:
        df = em.cargar(Path(args.csv))
    except em.ErrorFatal as e:
        log.error("%s", e)
        return 2

    nombre = nombre_modelo_final()
    log.info("Modelo: %s + nivel | %d posts de %d cuentas | bootstrap de cuentas con %d repeticiones…",
             nombre, len(df), df["username"].nunique(), args.repeticiones)
    boot = bootstrap_coeficientes(df, nombre, args.repeticiones)
    tabla = resumir_coeficientes(df, nombre, boot)
    efectos = efectos_dentro_de_cuentas(df)

    for _, f in tabla.iterrows():
        log.info("  %-26s %+6.1f%%  [%+6.1f%%, %+6.1f%%]  %s", f["variable"], f["efecto_%"],
                 f["efecto_bajo_%"], f["efecto_alto_%"], f["conclusion"])
    log.info("Recomendaciones:")
    for _, f in tabla[(tabla["conclusion"] != "sin evidencia suficiente") & tabla["accionable"]].iterrows():
        log.info("  • %s (%+.0f%%)", f["recomendacion"], f["efecto_%"])

    tabla.to_csv(RAIZ / "data" / "interpretacion_coeficientes.csv", index=False, encoding="utf-8-sig")
    efectos.to_csv(RAIZ / "data" / "efectos_dentro_de_cuentas.csv", index=False, encoding="utf-8-sig")
    log.info("Guardado: data/interpretacion_coeficientes.csv y data/efectos_dentro_de_cuentas.csv")
    log.info("Log completo en %s", archivo_log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
