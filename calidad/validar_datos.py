"""
analisis-barberias-peluquerias — control de calidad de los datos

Revisa que la tabla de posts (data/posts_features.csv) cumpla las reglas que el resto del proyecto da por hechas:
columnas y tipos, rangos, valores permitidos y coherencia entre columnas. Cada regla termina en uno de cuatro
estados:

    OK        se cumple
    ERROR     no se cumple: los datos no se deben usar (los scripts se detienen)
    AVISO     algo inusual que conviene revisar, pero no impide seguir
    OMITIDA   la tabla no tiene las columnas que la regla necesita

La usan preparar_features.py (antes de guardar) y entrenar_modelo.py (antes de entrenar). También se puede correr
sola:

    python -m calidad.validar_datos
    python -m calidad.validar_datos --csv data/posts_features.csv --min-posts 10

Cada corrida deja un reporte en reportes/calidad/ (Git lo ignora, porque puede nombrar posts y cuentas).
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

RAIZ = Path(__file__).resolve().parents[1]
CARPETA_REPORTES = RAIZ / "reportes" / "calidad"

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
FRANJAS = {"madrugada": (0, 5), "manana": (6, 11), "tarde": (12, 17), "noche": (18, 23)}
FORMATOS = {"reel", "carrusel", "imagen", "video_feed"}
BOOLEANAS = ["es_fin_de_semana", "tiene_pregunta", "tiene_llamado_accion", "menciona_precio_promo"]
CONTEOS = ["largo_texto_propio", "palabras_texto_propio", "hashtags_propios", "hashtags_total", "emojis_propios",
           "posts_ultimos_7_dias"]
# Columnas que pueden venir vacías: primer post de cada cuenta, posts sin dato de comentarios, posts sin texto propio
PUEDEN_SER_VACIAS = {"dias_desde_post_anterior", "comentarios", "log_comentarios", "texto_propio"}
MIN_CUENTAS_AVISO = 10
MAX_PARTE_CUENTA_AVISO = 0.40
MAX_EJEMPLOS = 5

OK, ERROR, AVISO, OMITIDA = "OK", "ERROR", "AVISO", "OMITIDA"


@dataclass
class Regla:
    nombre: str
    descripcion: str
    estado: str
    filas: int = 0
    detalle: str = ""
    ejemplos: list[str] = field(default_factory=list)


@dataclass
class Validacion:
    filas: int
    cuentas: int
    reglas: list[Regla]

    @property
    def errores(self) -> list[Regla]:
        return [r for r in self.reglas if r.estado == ERROR]

    @property
    def avisos(self) -> list[Regla]:
        return [r for r in self.reglas if r.estado == AVISO]

    @property
    def ok(self) -> bool:
        return not self.errores

    def resumen(self) -> str:
        cuenta = {e: sum(r.estado == e for r in self.reglas) for e in (OK, ERROR, AVISO, OMITIDA)}
        return (f"Validación de datos: {cuenta[OK]} OK, {cuenta[ERROR]} errores, {cuenta[AVISO]} avisos, "
                f"{cuenta[OMITIDA]} omitidas ({self.filas} posts de {self.cuentas} cuentas)")

    def lineas_problemas(self) -> list[str]:
        return [f"{r.estado}: {r.descripcion} — {r.detalle}" for r in self.errores + self.avisos]


# --------------------------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------------------------

def _como_booleana(s: pd.Series) -> pd.Series | None:
    """Convierte a bool aceptando True/False, 1/0 y 'true'/'false'. None si hay otros valores."""
    if s.dtype == bool:
        return s
    texto = s.astype(str).str.strip().str.lower()
    valido = texto.isin(["true", "false", "1", "0", "1.0", "0.0"])
    if not valido.all():
        return None
    return texto.isin(["true", "1", "1.0"])


def _ejemplos(df: pd.DataFrame, mascara: pd.Series) -> list[str]:
    columna = "post_id" if "post_id" in df.columns else None
    filas = df.loc[mascara]
    valores = filas[columna] if columna else pd.Series(filas.index)
    return [str(v) for v in valores.head(MAX_EJEMPLOS)]


def _es_entero(s: pd.Series) -> pd.Series:
    numeros = pd.to_numeric(s, errors="coerce")
    return numeros.notna() & np.isclose(numeros, np.round(numeros))


# --------------------------------------------------------------------------------------------
# Reglas: cada una recibe la tabla y devuelve (estado, filas afectadas, detalle, máscara de filas malas)
# --------------------------------------------------------------------------------------------

Resultado = tuple[str, int, str, "pd.Series | None"]


def _filas_malas(mascara: pd.Series, detalle_ok: str, detalle_mal: str, severidad: str = ERROR) -> Resultado:
    n = int(mascara.sum())
    return (severidad if n else OK, n, detalle_mal.format(n=n) if n else detalle_ok, mascara if n else None)


def r_post_id(df):
    malas = df["post_id"].isna() | df["post_id"].astype(str).str.strip().eq("") | df["post_id"].duplicated(keep=False)
    return _filas_malas(malas, "todos únicos", "{n} posts sin id o con id repetido")


def r_username(df):
    malas = df["username"].isna() | df["username"].astype(str).str.strip().eq("")
    return _filas_malas(malas, "todos con cuenta", "{n} posts sin cuenta")


def r_fechas(df):
    fechas = pd.to_datetime(df["fecha_publicacion_utc"], errors="coerce")
    futuro = pd.Timestamp.now() + pd.Timedelta(days=1)
    malas = fechas.isna() | (fechas > futuro)
    detalle = (f"del {fechas.min():%d-%m-%Y} al {fechas.max():%d-%m-%Y}" if fechas.notna().any() else "")
    return _filas_malas(malas, detalle, "{n} fechas vacías, inválidas o en el futuro")


def r_likes(df):
    likes = pd.to_numeric(df["likes"], errors="coerce")
    malas = likes.isna() | (likes < 0) | ~_es_entero(df["likes"])
    return _filas_malas(malas, f"entre {likes.min():.0f} y {likes.max():.0f}", "{n} posts con likes vacíos, negativos o no enteros")


def r_log_likes(df):
    likes = pd.to_numeric(df["likes"], errors="coerce")
    log_likes = pd.to_numeric(df["log_likes"], errors="coerce")
    malas = log_likes.isna() | ~np.isclose(log_likes, np.log1p(likes), atol=1e-6)
    return _filas_malas(malas, "log_likes = ln(1 + likes) en todos", "{n} posts donde log_likes ≠ ln(1 + likes)")


def r_comentarios(df):
    com = pd.to_numeric(df["comentarios"], errors="coerce")
    negativas = com < 0
    if negativas.any():
        return _filas_malas(negativas, "", "{n} posts con comentarios negativos")
    vacias = com.isna()
    return _filas_malas(vacias, "sin vacíos ni negativos", "{n} posts sin dato de comentarios", severidad=AVISO)


def r_seguidores(df):
    seg = pd.to_numeric(df["seguidores"], errors="coerce")
    malas = seg.isna() | (seg <= 0)
    if "log_seguidores" in df.columns and not malas.any():
        malas = ~np.isclose(pd.to_numeric(df["log_seguidores"], errors="coerce"), np.log(seg), atol=1e-6)
        return _filas_malas(malas, "positivos y coherentes con log_seguidores", "{n} posts donde log_seguidores ≠ ln(seguidores)")
    return _filas_malas(malas, "todos positivos", "{n} posts con seguidores vacíos o ≤ 0")


def r_log_seguidores(df):
    valores = pd.to_numeric(df["log_seguidores"], errors="coerce")
    malas = valores.isna() | ~np.isfinite(valores.fillna(np.inf)) | (valores < 0)
    return _filas_malas(malas, "todos finitos y ≥ 0", "{n} posts con log_seguidores vacío, infinito o negativo")


def r_hora(df):
    hora = pd.to_numeric(df["hora_local"], errors="coerce")
    malas = hora.isna() | ~_es_entero(df["hora_local"]) | (hora < 0) | (hora > 23)
    return _filas_malas(malas, "todas entre 0 y 23", "{n} posts con hora fuera de 0-23")


def r_dia(df):
    dia = pd.to_numeric(df["dia_semana"], errors="coerce")
    malas = dia.isna() | ~_es_entero(df["dia_semana"]) | (dia < 0) | (dia > 6)
    if not malas.any() and "nombre_dia" in df.columns:
        malas = df["nombre_dia"].astype(str) != dia.astype(int).map(lambda d: DIAS[d])
    if not malas.any() and "es_fin_de_semana" in df.columns:
        finde = _como_booleana(df["es_fin_de_semana"])
        if finde is not None:
            malas = finde != (dia >= 5)
    return _filas_malas(malas, "día, nombre y fin de semana coherentes", "{n} posts con el día de la semana incoherente")


def r_franja(df):
    validas = df["franja"].isin(FRANJAS)
    if not validas.all():
        return _filas_malas(~validas, "", "{n} posts con una franja que no es madrugada, manana, tarde ni noche")
    if "hora_local" not in df.columns:
        return (OK, 0, "valores válidos", None)
    hora = pd.to_numeric(df["hora_local"], errors="coerce")
    desde = df["franja"].map(lambda f: FRANJAS[f][0])
    hasta = df["franja"].map(lambda f: FRANJAS[f][1])
    malas = (hora < desde) | (hora > hasta)
    return _filas_malas(malas, "coherente con la hora en todos", "{n} posts donde la franja no calza con la hora")


def r_formato(df):
    desconocidos = ~df["formato"].isin(FORMATOS)
    vacios = df["formato"].isna()
    if vacios.any():
        return _filas_malas(vacios, "", "{n} posts sin formato")
    reparto = ", ".join(f"{k} {v:.0%}" for k, v in df["formato"].value_counts(normalize=True).items())
    return _filas_malas(desconocidos, reparto, "{n} posts con un formato no reconocido (quedan como imagen en el modelo)",
                        severidad=AVISO)


def r_conteos(df):
    columnas = [c for c in CONTEOS if c in df.columns]
    malas = pd.Series(False, index=df.index)
    for c in columnas:
        valores = pd.to_numeric(df[c], errors="coerce")
        malas |= valores.isna() | (valores < 0) | ~_es_entero(df[c])
    if {"hashtags_propios", "hashtags_total"} <= set(df.columns) and not malas.any():
        malas = pd.to_numeric(df["hashtags_propios"]) > pd.to_numeric(df["hashtags_total"])
        return _filas_malas(malas, f"{len(columnas)} columnas enteras y ≥ 0", "{n} posts con más hashtags propios que totales")
    return _filas_malas(malas, f"{len(columnas)} columnas enteras y ≥ 0", "{n} posts con conteos vacíos, negativos o no enteros")


def r_booleanas(df):
    columnas = [c for c in BOOLEANAS if c in df.columns]
    malas = [c for c in columnas if _como_booleana(df[c]) is None]
    if malas:
        return (ERROR, len(malas), f"columnas con valores que no son verdadero/falso: {', '.join(malas)}", None)
    return (OK, 0, f"{len(columnas)} columnas verdadero/falso", None)


def r_ritmo(df):
    dias = pd.to_numeric(df["dias_desde_post_anterior"], errors="coerce")
    malas = dias < 0
    return _filas_malas(malas, "sin intervalos negativos", "{n} posts con días desde el post anterior negativos")


def r_engagement(df):
    valores = pd.to_numeric(df["engagement_rate"], errors="coerce")
    malas = valores.isna() | ~np.isfinite(valores.fillna(np.inf)) | (valores < 0)
    return _filas_malas(malas, "todos finitos y ≥ 0", "{n} posts con engagement vacío, infinito o negativo")


def r_peso(df):
    peso = pd.to_numeric(df["peso_cuenta"], errors="coerce")
    if peso.isna().any() or (peso <= 0).any():
        return _filas_malas(peso.isna() | (peso <= 0), "", "{n} posts con peso vacío o ≤ 0")
    total_por_cuenta = peso.groupby(df["username"]).sum()
    if not np.isclose(peso.mean(), 1, atol=1e-6) or not np.allclose(total_por_cuenta, total_por_cuenta.iloc[0]):
        return (ERROR, 0, f"el promedio es {peso.mean():.4f} (debe ser 1) o las cuentas no pesan lo mismo", None)
    return (OK, 0, "promedio 1 y mismo peso total por cuenta", None)


def r_vacios(df, columnas):
    revisar = [c for c in columnas if c in df.columns and c not in PUEDEN_SER_VACIAS]
    vacias = {c: int(df[c].isna().sum()) for c in revisar if df[c].isna().any()}
    if vacias:
        malas = df[list(vacias)].isna().any(axis=1)
        detalle = ", ".join(f"{c} ({n})" for c, n in vacias.items())
        return (ERROR, int(malas.sum()), f"valores vacíos en: {detalle}", malas)
    return (OK, 0, f"{len(revisar)} columnas sin vacíos", None)


# --------------------------------------------------------------------------------------------
# Validación completa
# --------------------------------------------------------------------------------------------

REGLAS: list[tuple[str, str, list[str], Callable]] = [
    ("post_id", "Cada post tiene un id único", ["post_id"], r_post_id),
    ("cuenta", "Cada post tiene cuenta", ["username"], r_username),
    ("fechas", "Fechas válidas y no en el futuro", ["fecha_publicacion_utc"], r_fechas),
    ("likes", "Likes enteros y ≥ 0", ["likes"], r_likes),
    ("log_likes", "El target log_likes es ln(1 + likes)", ["likes", "log_likes"], r_log_likes),
    ("comentarios", "Comentarios ≥ 0", ["comentarios"], r_comentarios),
    ("seguidores", "Seguidores positivos", ["seguidores"], r_seguidores),
    ("log_seguidores", "log_seguidores finito", ["log_seguidores"], r_log_seguidores),
    ("hora", "Hora local entre 0 y 23", ["hora_local"], r_hora),
    ("dia", "Día de la semana coherente", ["dia_semana"], r_dia),
    ("franja", "Franja válida y coherente con la hora", ["franja"], r_franja),
    ("formato", "Formato reconocido", ["formato"], r_formato),
    ("conteos", "Conteos de texto enteros y ≥ 0", ["largo_texto_propio"], r_conteos),
    ("booleanas", "Columnas verdadero/falso", ["tiene_llamado_accion"], r_booleanas),
    ("ritmo", "Días desde el post anterior ≥ 0", ["dias_desde_post_anterior"], r_ritmo),
    ("engagement", "Engagement finito y ≥ 0", ["engagement_rate"], r_engagement),
    ("peso", "Peso por cuenta con promedio 1", ["peso_cuenta", "username"], r_peso),
]


def validar_posts(df: pd.DataFrame, columnas_requeridas: list[str] | None = None, min_posts: int | None = None,
                  meses: int | None = None, min_edad_dias: float | None = None) -> Validacion:
    """Aplica todas las reglas. `columnas_requeridas` por defecto son las que escribe preparar_features.py."""
    if columnas_requeridas is None:
        from preparar_features import COLUMNAS_SALIDA     # import tardío: evita una dependencia circular
        columnas_requeridas = list(COLUMNAS_SALIDA)
    reglas: list[Regla] = []
    faltan = [c for c in columnas_requeridas if c not in df.columns]
    reglas.append(Regla("columnas", "Están todas las columnas necesarias", ERROR if faltan else OK, len(faltan),
                        f"faltan: {', '.join(faltan)}" if faltan else f"{len(columnas_requeridas)} columnas"))
    if df.empty:
        reglas.append(Regla("filas", "La tabla tiene posts", ERROR, 0, "no hay filas"))
        return Validacion(0, 0, reglas)

    estado, n, detalle, malas = r_vacios(df, columnas_requeridas)
    reglas.append(Regla("vacios", "Sin valores vacíos en columnas obligatorias", estado, n, detalle,
                        _ejemplos(df, malas) if malas is not None else []))

    for nombre, descripcion, necesita, regla in REGLAS:
        if not set(necesita) <= set(df.columns):
            reglas.append(Regla(nombre, descripcion, OMITIDA, 0, f"falta {', '.join(sorted(set(necesita) - set(df.columns)))}"))
            continue
        try:
            with np.errstate(all="ignore"):          # los valores inválidos se reportan como hallazgos, no como avisos
                estado, n, detalle, malas = regla(df)
        except Exception as e:      # una regla que no se puede evaluar es un problema de los datos, no del script
            estado, n, detalle, malas = ERROR, 0, f"no se pudo evaluar ({type(e).__name__}: {e})", None
        reglas.append(Regla(nombre, descripcion, estado, n, detalle,
                            _ejemplos(df, malas) if malas is not None else []))

    reglas += _reglas_de_configuracion(df, min_posts, meses, min_edad_dias)
    reglas += _reglas_de_composicion(df)
    cuentas = df["username"].nunique() if "username" in df.columns else 0
    return Validacion(len(df), cuentas, reglas)


def _reglas_de_configuracion(df, min_posts, meses, min_edad_dias) -> list[Regla]:
    """Reglas que dependen de las opciones con que se corrió preparar_features.py."""
    reglas = []
    if min_posts is not None and "username" in df.columns:
        por_cuenta = df["username"].value_counts()
        pocas = por_cuenta[por_cuenta < min_posts]
        reglas.append(Regla("min_posts", f"Cada cuenta tiene al menos {min_posts} posts", ERROR if len(pocas) else OK,
                            len(pocas), f"{len(pocas)} cuentas bajo el mínimo" if len(pocas) else
                            f"mínimo por cuenta: {por_cuenta.min()}"))
    if "edad_dias_captura" in df.columns and (min_edad_dias is not None or meses is not None):
        edad = pd.to_numeric(df["edad_dias_captura"], errors="coerce")
        if min_edad_dias is not None:
            malas = edad.isna() | (edad < min_edad_dias)
            estado, n, detalle, m = _filas_malas(malas, f"todos con al menos {min_edad_dias:g} días",
                                                 f"{{n}} posts con menos de {min_edad_dias:g} días al leer sus likes")
            reglas.append(Regla("edad", "Antigüedad mínima al leer los likes", estado, n, detalle,
                                _ejemplos(df, m) if m is not None else []))
        if meses is not None:
            dias_ventana = round(30.44 * meses)
            malas = edad > dias_ventana + 1e-6
            estado, n, detalle, m = _filas_malas(malas, f"todos dentro de {meses} meses",
                                                 f"{{n}} posts fuera de la ventana de {meses} meses")
            reglas.append(Regla("ventana", "Posts dentro de la ventana", estado, n, detalle,
                                _ejemplos(df, m) if m is not None else []))
    return reglas


def _reglas_de_composicion(df) -> list[Regla]:
    """Avisos sobre la composición del dataset (no son errores: pueden ser reales)."""
    if "username" not in df.columns:
        return []
    por_cuenta = df["username"].value_counts()
    reglas = [Regla("n_cuentas", f"Al menos {MIN_CUENTAS_AVISO} cuentas", AVISO if len(por_cuenta) < MIN_CUENTAS_AVISO else OK,
                    0, f"{len(por_cuenta)} cuentas")]
    parte = por_cuenta.iloc[0] / len(df)
    reglas.append(Regla("concentracion", f"Ninguna cuenta con más del {MAX_PARTE_CUENTA_AVISO:.0%} de los posts",
                        AVISO if parte > MAX_PARTE_CUENTA_AVISO else OK, 0,
                        f"la cuenta más grande tiene el {parte:.0%} de los posts"))
    return reglas


# --------------------------------------------------------------------------------------------
# Reporte
# --------------------------------------------------------------------------------------------

def guardar_reporte(validacion: Validacion, origen: str, carpeta: Path | None = None) -> Path:
    """Escribe el reporte en Markdown y devuelve su ruta."""
    carpeta = Path(carpeta or CARPETA_REPORTES)
    carpeta.mkdir(parents=True, exist_ok=True)
    ahora = datetime.now()
    ruta = carpeta / f"validacion_{origen}_{ahora:%Y%m%d_%H%M%S}.md"
    iconos = {OK: "✅", ERROR: "❌", AVISO: "⚠️", OMITIDA: "➖"}
    lineas = [
        f"# Validación de datos — {origen}",
        "",
        f"{ahora:%d-%m-%Y %H:%M:%S} · {validacion.filas} posts de {validacion.cuentas} cuentas",
        "",
        f"**Resultado: {'APROBADA' if validacion.ok else 'RECHAZADA'}** — {validacion.resumen()}",
        "",
        "| Estado | Regla | Filas | Detalle | Ejemplos (post_id) |",
        "|---|---|---|---|---|",
    ]
    orden = {ERROR: 0, AVISO: 1, OK: 2, OMITIDA: 3}
    for r in sorted(validacion.reglas, key=lambda r: orden[r.estado]):
        lineas.append(f"| {iconos[r.estado]} {r.estado} | {r.descripcion} | {r.filas or ''} | "
                      f"{r.detalle.replace('|', '/')} | {', '.join(r.ejemplos)} |")
    ruta.write_text("\n".join(lineas) + "\n", encoding="utf-8")
    return ruta


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Valida data/posts_features.csv y deja un reporte en reportes/calidad/.")
    p.add_argument("--csv", default=str(RAIZ / "data" / "posts_features.csv"))
    p.add_argument("--min-posts", type=int, help="Posts mínimos por cuenta (el valor que usaste en preparar_features)")
    p.add_argument("--meses", type=int, default=12)
    p.add_argument("--min-edad-dias", type=float, default=7)
    args = p.parse_args(argv)
    ruta_csv = Path(args.csv)
    if not ruta_csv.exists():
        print(f"No existe {ruta_csv}. Corre primero preparar_features.py.")
        return 2
    df = pd.read_csv(ruta_csv, encoding="utf-8-sig", dtype={"post_id": str})
    validacion = validar_posts(df, min_posts=args.min_posts, meses=args.meses, min_edad_dias=args.min_edad_dias)
    ruta = guardar_reporte(validacion, "manual")
    print(validacion.resumen())
    for linea in validacion.lineas_problemas():
        print("  " + linea)
    print(f"Reporte: {ruta}")
    return 0 if validacion.ok else 1


if __name__ == "__main__":
    sys.path.insert(0, str(RAIZ))
    sys.exit(main())
