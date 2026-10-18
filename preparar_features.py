"""
analisis-barberias-peluquerias — Fase 2: limpieza y features

Lee los posts extraídos en la Fase 1, los limpia y calcula las features y el target del modelo.

Guarda el resultado en:
  - SQL Server: dbo.PostsFeatures y dbo.CuentasResumen (antes ejecuta sql/03_features.sql en SSMS)
  - data/posts_features.csv y data/cuentas_resumen.csv (para la Fase 3)

Antes de guardar, valida los datos con calidad/validar_datos.py. Si no pasan, no guarda nada y termina con código 3.
El reporte de cada validación queda en reportes/calidad/.

Uso:
    python preparar_features.py
    python preparar_features.py --min-posts 15 --min-edad-dias 7 --meses 12
    python preparar_features.py --desde-jsonl salida_prueba\\20261014_224833     # sin SQL Server
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from dotenv import load_dotenv

from calidad.validar_datos import guardar_reporte, validar_posts

RAIZ = Path(__file__).resolve().parent
ZONA_CHILE = ZoneInfo("America/Santiago")
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

# Una línea del caption es "plantilla" si la cuenta la repite en al menos el 10% de sus posts
# y en 5 posts o más (direcciones, teléfonos, firmas, bloques de hashtags, equipo de barberos...).
UMBRAL_PLANTILLA = 0.10
MIN_REPETICIONES_PLANTILLA = 5
MIN_POSTS_PARA_PLANTILLA = 10

RE_HASHTAG = re.compile(r"#\w+")
RE_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]")
RE_PREGUNTA = re.compile(r"[?¿]|\b(?:comenta\w*|coment[aá]rios?|elige|elijan|cu[eé]ntanos|etiqueta\w*)\b", re.I)
RE_LLAMADO = re.compile(r"\b(?:ag[eé]nd\w*|reserv\w*|link en (?:la )?bio|escr[ií]be\w*|wsp|whatsapp|inbox|"
                        r"ll[aá]ma(?:nos)?|cupos?|horas? disponibles?)\b", re.I)
RE_PRECIO = re.compile(r"\$\s?\d|\b(?:precios?|promo\w*|descuentos?|ofertas?|2x1|gratis)\b", re.I)
RE_SORTEO = re.compile(r"\b(?:sorteo|sorteamos|concurso|giveaway|ganador|ganadora|participa|participar)\b", re.I)

COLUMNAS_ENTRADA = ["post_id", "username", "caption", "publicacion_utc", "media_type", "media_product_type",
                    "likes", "comentarios", "captura_utc", "seguidores", "estado_lista"]

# Columnas de salida (CSV) y su nombre en SQL Server, en el mismo orden que dbo.PostsFeatures
COLUMNAS_SALIDA = {
    "post_id": "PostId", "username": "Username", "fecha_publicacion_utc": "FechaPublicacionUtc",
    "fecha_local": "FechaLocal", "anio": "Anio", "mes": "Mes", "dia_semana": "DiaSemana",
    "nombre_dia": "NombreDia", "es_fin_de_semana": "EsFinDeSemana", "hora_local": "HoraLocal",
    "franja": "Franja", "formato": "Formato", "texto_propio": "TextoPropio",
    "largo_texto_propio": "LargoTextoPropio", "palabras_texto_propio": "PalabrasTextoPropio",
    "hashtags_propios": "HashtagsPropios", "hashtags_total": "HashtagsTotal", "emojis_propios": "EmojisPropios",
    "tiene_pregunta": "TienePregunta", "tiene_llamado_accion": "TieneLlamadoAccion",
    "menciona_precio_promo": "MencionaPrecioPromo", "dias_desde_post_anterior": "DiasDesdePostAnterior",
    "posts_ultimos_7_dias": "PostsUltimos7Dias", "seguidores": "Seguidores", "log_seguidores": "LogSeguidores",
    "edad_dias_captura": "EdadDiasCaptura", "likes": "Likes", "comentarios": "Comentarios",
    "log_likes": "LogLikes", "log_comentarios": "LogComentarios", "engagement_rate": "EngagementRate",
    "peso_cuenta": "PesoCuenta",
}
COLUMNAS_CUENTAS = {
    "username": "Username", "estado_lista": "EstadoLista", "seguidores": "Seguidores",
    "posts_extraidos": "PostsExtraidos", "posts_en_ventana": "PostsEnVentana",
    "posts_muy_recientes": "PostsMuyRecientes", "posts_sin_likes": "PostsSinLikes", "sorteos": "Sorteos",
    "posts_validos": "PostsValidos", "lineas_plantilla": "LineasPlantilla", "dias_sin_publicar": "DiasSinPublicar",
    "incluida": "Incluida", "motivo_exclusion": "MotivoExclusion",
}

log = logging.getLogger("features")


class ErrorFatal(Exception):
    pass


class DatosInvalidos(ErrorFatal):
    """Los datos no pasaron el control de calidad (calidad/validar_datos.py)."""


# --------------------------------------------------------------------------------------------
# Carga
# --------------------------------------------------------------------------------------------

def cargar_desde_sql(conexion: str) -> pd.DataFrame:
    try:
        import pyodbc
    except ImportError:
        raise ErrorFatal("Falta pyodbc. Ejecuta: pip install -r requirements.txt") from None
    try:
        cn = pyodbc.connect(conexion)
    except pyodbc.Error as e:
        raise ErrorFatal(f"No se pudo conectar a SQL Server. Revisa SQL_CONEXION en .env. Detalle: {e}") from None
    try:
        filas = cn.cursor().execute(
            """SELECT p.PostId, p.Username, p.Caption, p.FechaPublicacionUtc, p.MediaType, p.MediaProductType,
                      p.Likes, p.Comentarios, p.FechaCapturaUtc, c.Seguidores, c.EstadoLista
               FROM dbo.Posts p
               JOIN dbo.Cuentas c ON c.Username = p.Username
               WHERE c.EstadoExtraccion = N'ok'""").fetchall()
    finally:
        cn.close()
    return pd.DataFrame.from_records([tuple(f) for f in filas], columns=COLUMNAS_ENTRADA)


def cargar_desde_jsonl(carpeta: Path) -> pd.DataFrame:
    """Lee la salida de `extraer_instagram.py --probar` (posts.jsonl y cuentas.jsonl)."""
    if not (carpeta / "posts.jsonl").exists():
        raise ErrorFatal(f"No hay posts.jsonl en {carpeta}")
    posts = pd.read_json(carpeta / "posts.jsonl", lines=True, dtype=False)
    cuentas = pd.read_json(carpeta / "cuentas.jsonl", lines=True, dtype=False)
    cuentas = cuentas[cuentas["estado_extraccion"] == "ok"].rename(columns={"estado": "estado_lista"})
    if "estado_lista" not in cuentas:
        cuentas["estado_lista"] = None
    cuentas = cuentas.drop_duplicates("username", keep="last")
    df = posts.merge(cuentas[["username", "seguidores", "estado_lista"]], on="username", how="inner")
    return df[COLUMNAS_ENTRADA]


def preparar_tipos(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in ("publicacion_utc", "captura_utc"):
        fechas = pd.to_datetime(df[col])
        df[col] = fechas.dt.tz_localize("UTC") if fechas.dt.tz is None else fechas.dt.tz_convert("UTC")
    df["post_id"] = df["post_id"].astype(str)
    df["caption"] = df["caption"].fillna("").astype(str)
    for col in ("likes", "comentarios", "seguidores"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return (df.drop_duplicates("post_id", keep="last")
              .sort_values(["username", "publicacion_utc"])
              .reset_index(drop=True))


# --------------------------------------------------------------------------------------------
# Texto
# --------------------------------------------------------------------------------------------

def normalizar_linea(linea: str) -> str:
    return re.sub(r"\s+", " ", linea.strip().lower())


def detectar_plantilla(captions: list[str]) -> set[str]:
    """Líneas que la cuenta repite en muchos posts (dirección, teléfono, firma, hashtags fijos)."""
    captions = [c for c in captions if c]
    if len(captions) < MIN_POSTS_PARA_PLANTILLA:
        return set()
    conteo = Counter()
    for caption in captions:
        conteo.update({normalizar_linea(l) for l in caption.splitlines() if l.strip()})
    minimo = max(MIN_REPETICIONES_PLANTILLA, math.ceil(UMBRAL_PLANTILLA * len(captions)))
    return {linea for linea, veces in conteo.items() if veces >= minimo}


def quitar_plantilla(caption: str, plantilla: set[str]) -> str:
    return "\n".join(l.strip() for l in caption.splitlines()
                     if l.strip() and normalizar_linea(l) not in plantilla)


def formato(media_type: str | None, producto: str | None) -> str:
    if producto == "REELS":
        return "reel"
    return {"CAROUSEL_ALBUM": "carrusel", "IMAGE": "imagen", "VIDEO": "video_feed"}.get(media_type or "", "otro")


def franja(hora: int) -> str:
    if hora < 6:
        return "madrugada"
    if hora < 12:
        return "manana"
    if hora < 18:
        return "tarde"
    return "noche"


def posts_previos(fechas: pd.Series, dias: int = 7) -> np.ndarray:
    """Para cada post, cuántos posts publicó la cuenta en los `dias` anteriores (fechas ya ordenadas)."""
    t = fechas.dt.tz_convert(None).to_numpy(dtype="datetime64[ns]")
    inicio = np.searchsorted(t, t - np.timedelta64(dias, "D"), side="left")
    return np.arange(len(t)) - inicio


# --------------------------------------------------------------------------------------------
# Limpieza y features
# --------------------------------------------------------------------------------------------

@dataclass
class Resultado:
    posts: pd.DataFrame
    cuentas: pd.DataFrame
    embudo: list[tuple[str, int]]


def construir_features(df: pd.DataFrame, meses: int = 12, min_edad_dias: float = 7,
                       min_posts: int = 20) -> Resultado:
    df = preparar_tipos(df)

    # 1. Texto propio: se quita la plantilla de cada cuenta (detectada con todos sus posts)
    plantillas = {u: detectar_plantilla(g["caption"].tolist()) for u, g in df.groupby("username", sort=False)}
    df["texto_propio"] = [quitar_plantilla(c, plantillas[u]) for c, u in zip(df["caption"], df["username"])]

    # 2. Ritmo de publicación (con todos los posts de la cuenta, no solo los válidos)
    df["dias_desde_post_anterior"] = (df.groupby("username", sort=False)["publicacion_utc"].diff()
                                      .dt.total_seconds() / 86400).round(3)
    df["posts_ultimos_7_dias"] = df.groupby("username", sort=False)["publicacion_utc"].transform(posts_previos)

    # 3. Edad del post al momento de leer sus likes y marcas de filtro
    df["edad_dias_captura"] = ((df["captura_utc"] - df["publicacion_utc"]).dt.total_seconds() / 86400).round(3)
    df["es_sorteo"] = df["caption"].str.contains(RE_SORTEO, regex=True)
    dias_ventana = round(30.44 * meses)
    en_ventana = df["publicacion_utc"] >= df["captura_utc"] - pd.Timedelta(days=dias_ventana)
    suficiente_edad = df["edad_dias_captura"] >= min_edad_dias
    con_likes = df["likes"].notna()
    no_sorteo = ~df["es_sorteo"]

    embudo = [("Posts extraídos", len(df))]
    paso = en_ventana
    embudo.append((f"Publicados en los últimos {meses} meses", int(paso.sum())))
    paso = paso & suficiente_edad
    embudo.append((f"Con al menos {min_edad_dias:g} días de antigüedad", int(paso.sum())))
    paso = paso & con_likes
    embudo.append(("Con likes visibles", int(paso.sum())))
    paso = paso & no_sorteo
    embudo.append(("Sin sorteos ni concursos", int(paso.sum())))

    # 4. Resumen por cuenta y exclusión de cuentas con pocos posts válidos o sin seguidores
    df["_ventana"], df["_valido"] = en_ventana, paso
    df["_reciente"] = en_ventana & ~suficiente_edad
    df["_sin_likes"] = en_ventana & suficiente_edad & ~con_likes
    df["_sorteo"] = en_ventana & suficiente_edad & con_likes & ~no_sorteo
    g = df.groupby("username", sort=True)
    cuentas = pd.DataFrame({
        "estado_lista": g["estado_lista"].last(),
        "seguidores": g["seguidores"].last(),
        "posts_extraidos": g.size(),
        "posts_en_ventana": g["_ventana"].sum(),
        "posts_muy_recientes": g["_reciente"].sum(),
        "posts_sin_likes": g["_sin_likes"].sum(),
        "sorteos": g["_sorteo"].sum(),
        "posts_validos": g["_valido"].sum(),
        "dias_sin_publicar": ((g["captura_utc"].max() - g["publicacion_utc"].max()).dt.total_seconds() // 86400),
    }).reset_index()
    cuentas["lineas_plantilla"] = cuentas["username"].map(lambda u: len(plantillas[u]))
    for col in ("seguidores", "dias_sin_publicar"):
        cuentas[col] = cuentas[col].round().astype("Int64")      # enteros que admiten vacíos (columnas INT)

    def motivo(fila) -> str | None:
        if pd.isna(fila["seguidores"]) or fila["seguidores"] <= 0:
            return "sin dato de seguidores"
        if fila["posts_en_ventana"] == 0:
            return f"sin posts en los últimos {meses} meses"
        if fila["posts_validos"] < min_posts:
            return f"solo {int(fila['posts_validos'])} posts válidos (mínimo {min_posts})"
        return None

    cuentas["motivo_exclusion"] = cuentas.apply(motivo, axis=1)
    cuentas["incluida"] = cuentas["motivo_exclusion"].isna()
    incluidas = set(cuentas.loc[cuentas["incluida"], "username"])
    finales = df[df["_valido"] & df["username"].isin(incluidas)].copy()
    embudo.append((f"De cuentas con al menos {min_posts} posts válidos", len(finales)))

    # 5. Features del post
    local = finales["publicacion_utc"].dt.tz_convert(ZONA_CHILE)
    finales["fecha_publicacion_utc"] = finales["publicacion_utc"].dt.tz_convert(None)
    finales["fecha_local"] = local.dt.tz_localize(None)
    finales["anio"] = local.dt.year
    finales["mes"] = local.dt.month
    finales["dia_semana"] = local.dt.dayofweek
    finales["nombre_dia"] = finales["dia_semana"].map(lambda d: DIAS[d])
    finales["es_fin_de_semana"] = finales["dia_semana"] >= 5
    finales["hora_local"] = local.dt.hour
    finales["franja"] = finales["hora_local"].map(franja)
    finales["formato"] = [formato(m, p) for m, p in zip(finales["media_type"], finales["media_product_type"])]

    texto = finales["texto_propio"]
    finales["largo_texto_propio"] = texto.str.len()
    finales["palabras_texto_propio"] = texto.str.split().str.len().fillna(0).astype(int)
    finales["hashtags_propios"] = texto.str.count(RE_HASHTAG.pattern)
    finales["hashtags_total"] = finales["caption"].str.count(RE_HASHTAG.pattern)
    finales["emojis_propios"] = texto.map(lambda t: len(RE_EMOJI.findall(t)))
    finales["tiene_pregunta"] = texto.str.contains(RE_PREGUNTA, regex=True)
    finales["tiene_llamado_accion"] = texto.str.contains(RE_LLAMADO, regex=True)
    finales["menciona_precio_promo"] = texto.str.contains(RE_PRECIO, regex=True)

    finales["seguidores"] = finales["seguidores"].astype(int)
    finales["log_seguidores"] = np.log(finales["seguidores"])
    finales["likes"] = finales["likes"].astype(int)
    finales["log_likes"] = np.log1p(finales["likes"])
    finales["log_comentarios"] = np.log1p(finales["comentarios"])
    finales["engagement_rate"] = (finales["likes"] + finales["comentarios"].fillna(0)) / finales["seguidores"]
    finales["comentarios"] = finales["comentarios"].round().astype("Int64")

    # 6. Peso por cuenta: cada cuenta pesa lo mismo en total, sin importar cuántos posts tenga.
    #    El promedio de los pesos es 1. Se usa como sample_weight en la Fase 4.
    por_cuenta = finales.groupby("username")["post_id"].transform("count")
    n_cuentas = finales["username"].nunique()
    finales["peso_cuenta"] = (len(finales) / max(n_cuentas, 1)) / por_cuenta

    finales["texto_propio"] = finales["texto_propio"].str.slice(0, 2200)
    posts = finales[list(COLUMNAS_SALIDA)].sort_values(["username", "fecha_publicacion_utc"]).reset_index(drop=True)
    return Resultado(posts=posts, cuentas=cuentas[list(COLUMNAS_CUENTAS)], embudo=embudo)


# --------------------------------------------------------------------------------------------
# Guardado
# --------------------------------------------------------------------------------------------

def a_nativo(v):
    """Convierte valores de pandas/numpy a tipos que pyodbc entiende."""
    if v is None or v is pd.NA or v is pd.NaT:
        return None
    if isinstance(v, (float, np.floating)):
        return None if math.isnan(v) else float(v)
    if isinstance(v, np.bool_):
        return bool(v)
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime().replace(microsecond=0)
    return v


def filas_para_sql(df: pd.DataFrame, columnas: list[str], extra: tuple = ()) -> list[tuple]:
    return [tuple(a_nativo(v) for v in fila) + extra for fila in df[columnas].itertuples(index=False, name=None)]


def guardar_sql(conexion: str, res: Resultado) -> None:
    try:
        import pyodbc
    except ImportError:
        raise ErrorFatal("Falta pyodbc. Ejecuta: pip install -r requirements.txt") from None
    generado = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    cn = pyodbc.connect(conexion, autocommit=False)
    try:
        cur = cn.cursor()
        faltan = [t for t in ("PostsFeatures", "CuentasResumen")
                  if cur.execute("SELECT OBJECT_ID(?, 'U')", f"dbo.{t}").fetchone()[0] is None]
        if faltan:
            raise ErrorFatal(f"Faltan tablas ({', '.join(faltan)}). Ejecuta sql/03_features.sql en SSMS.")

        cols_p = list(COLUMNAS_SALIDA.values()) + ["FechaGeneracionUtc"]
        cols_c = list(COLUMNAS_CUENTAS.values()) + ["FechaGeneracionUtc"]
        sql_p = f"INSERT INTO dbo.PostsFeatures ({', '.join(cols_p)}) VALUES ({', '.join('?' * len(cols_p))})"
        sql_c = f"INSERT INTO dbo.CuentasResumen ({', '.join(cols_c)}) VALUES ({', '.join('?' * len(cols_c))})"

        cur.execute("DELETE FROM dbo.PostsFeatures")
        cur.execute("DELETE FROM dbo.CuentasResumen")
        cur.fast_executemany = True
        filas_p = filas_para_sql(res.posts, list(COLUMNAS_SALIDA), (generado,))
        filas_c = filas_para_sql(res.cuentas, list(COLUMNAS_CUENTAS), (generado,))
        if filas_p:
            cur.executemany(sql_p, filas_p)
        if filas_c:
            cur.executemany(sql_c, filas_c)
        cn.commit()
    except pyodbc.Error as e:
        cn.rollback()
        raise ErrorFatal(f"Error al escribir en SQL Server: {e}") from None
    finally:
        cn.close()


def guardar_csv(res: Resultado, carpeta: Path) -> tuple[Path, Path]:
    carpeta.mkdir(parents=True, exist_ok=True)
    ruta_p, ruta_c = carpeta / "posts_features.csv", carpeta / "cuentas_resumen.csv"
    res.posts.to_csv(ruta_p, index=False, encoding="utf-8-sig")
    res.cuentas.to_csv(ruta_c, index=False, encoding="utf-8-sig")
    return ruta_p, ruta_c


# --------------------------------------------------------------------------------------------
# Reporte
# --------------------------------------------------------------------------------------------

def reportar(res: Resultado) -> None:
    log.info("Embudo de limpieza:")
    for nombre, n in res.embudo:
        log.info("  %-48s %6d", nombre, n)

    excluidas = res.cuentas[~res.cuentas["incluida"]]
    log.info("Cuentas incluidas: %d | excluidas: %d", int(res.cuentas["incluida"].sum()), len(excluidas))
    for _, c in excluidas.iterrows():
        log.info("  ✗ %-28s %s", c["username"], c["motivo_exclusion"])

    p = res.posts
    if p.empty:
        log.warning("No quedó ningún post válido. Revisa los parámetros (--min-posts, --meses).")
        return
    log.info("Likes por post: mediana %.0f | promedio %.0f | máximo %d",
             p["likes"].median(), p["likes"].mean(), p["likes"].max())
    formatos = p["formato"].value_counts(normalize=True).mul(100).round(0)
    log.info("Formatos: %s", ", ".join(f"{k} {v:.0f}%" for k, v in formatos.items()))
    log.info("Con pregunta: %.0f%% | con llamado a la acción: %.0f%% | menciona precio/promo: %.0f%%",
             100 * p["tiene_pregunta"].mean(), 100 * p["tiene_llamado_accion"].mean(),
             100 * p["menciona_precio_promo"].mean())
    log.info("Texto propio: mediana %.0f caracteres | %.0f%% de los posts sin texto propio",
             p["largo_texto_propio"].median(), 100 * (p["largo_texto_propio"] == 0).mean())
    top = res.cuentas[res.cuentas["incluida"]].nlargest(3, "posts_validos")
    log.info("Cuentas con más posts válidos: %s",
             ", ".join(f"{r.username} ({r.posts_validos})" for r in top.itertuples()))


def configurar_log() -> Path:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    carpeta = RAIZ / "logs"
    carpeta.mkdir(exist_ok=True)
    archivo = carpeta / f"features_{datetime.now():%Y%m%d_%H%M%S}.log"
    formato_log = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    consola = logging.StreamHandler(sys.stdout)
    consola.setFormatter(formato_log)
    en_archivo = logging.FileHandler(archivo, encoding="utf-8")
    en_archivo.setFormatter(formato_log)
    log.handlers[:] = [consola, en_archivo]
    log.setLevel(logging.INFO)
    return archivo


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Limpia los posts y calcula las features del modelo.")
    p.add_argument("--meses", type=int, default=12, help="Ventana de posts a considerar (por defecto 12)")
    p.add_argument("--min-edad-dias", type=float, default=7, help="Antigüedad mínima del post (por defecto 7)")
    p.add_argument("--min-posts", type=int, default=20, help="Posts válidos mínimos por cuenta (por defecto 20)")
    p.add_argument("--desde-jsonl", help="Carpeta de salida_prueba para probar sin SQL Server")
    p.add_argument("--sin-sql", action="store_true", help="Lee de SQL Server pero solo escribe los CSV")
    args = p.parse_args(argv)
    archivo_log = configurar_log()

    try:
        if args.desde_jsonl:
            carpeta = Path(args.desde_jsonl)
            datos = cargar_desde_jsonl(carpeta)
            log.info("Leídos %d posts desde %s", len(datos), carpeta)
        else:
            load_dotenv(RAIZ / ".env")
            conexion = os.getenv("SQL_CONEXION", "").strip()
            if not conexion:
                raise ErrorFatal("Falta SQL_CONEXION en .env.")
            datos = cargar_desde_sql(conexion)
            log.info("Leídos %d posts de %d cuentas desde SQL Server", len(datos), datos["username"].nunique())
        if datos.empty:
            raise ErrorFatal("No hay posts. Corre primero la Fase 1 (extraer_instagram.py).")

        res = construir_features(datos, args.meses, args.min_edad_dias, args.min_posts)
        reportar(res)

        # Control de calidad: si los datos no cumplen las reglas, no se guarda nada
        validacion = validar_posts(res.posts, columnas_requeridas=list(COLUMNAS_SALIDA), min_posts=args.min_posts,
                                   meses=args.meses, min_edad_dias=args.min_edad_dias)
        ruta_reporte = guardar_reporte(validacion, "features", RAIZ / "reportes" / "calidad")
        log.info("%s", validacion.resumen())
        for linea in validacion.lineas_problemas():
            log.warning("  %s", linea)
        if not validacion.ok:
            raise DatosInvalidos("Los datos no pasaron el control de calidad: no se guardó nada. "
                                 f"Detalle en {ruta_reporte}")

        destino = Path(args.desde_jsonl) if args.desde_jsonl else RAIZ / "data"
        ruta_p, ruta_c = guardar_csv(res, destino)
        log.info("CSV: %s y %s", ruta_p, ruta_c)
        if not args.desde_jsonl and not args.sin_sql:
            guardar_sql(conexion, res)
            log.info("SQL Server: %d filas en dbo.PostsFeatures y %d en dbo.CuentasResumen",
                     len(res.posts), len(res.cuentas))
    except DatosInvalidos as e:
        log.error("%s", e)
        return 3
    except ErrorFatal as e:
        log.error("%s", e)
        return 2
    log.info("Reporte de calidad: %s", ruta_reporte)
    log.info("Log completo en %s", archivo_log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
