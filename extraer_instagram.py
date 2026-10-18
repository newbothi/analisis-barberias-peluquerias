"""
analisis-barberias-peluquerias — Fase 1: extracción de datos

Lee la lista de barberías (CSV), consulta la API de Instagram (Business Discovery) para cada
username y guarda cuentas y posts públicos en SQL Server.

Uso:
    python extraer_instagram.py --probar                    # 1 cuenta, guarda en archivos (sin SQL Server)
    python extraer_instagram.py                             # todas las cuentas con username
    python extraer_instagram.py --usuarios cuenta.uno,cuenta.dos
    python extraer_instagram.py --pendientes                # salta las que salieron OK en las últimas 24 h
    python extraer_instagram.py --meses 6 --sin-dudosas
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol

import requests
from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent

CAMPOS_MEDIA = "id,caption,timestamp,media_type,media_product_type,like_count,comments_count,permalink"
TAM_PAGINA = 50
MAX_PAGINAS_POR_CUENTA = 60        # tope de seguridad: 3.000 posts por cuenta
PAUSA_ENTRE_LLAMADAS_S = 2.0
MAX_REINTENTOS = 4                 # errores de red o temporales de la API
MAX_ESPERAS_POR_LIMITE = 6         # veces seguidas que se espera por el límite de llamadas
ESPERA_LIMITE_POR_DEFECTO_S = 15 * 60

# Códigos de error de la Graph API
CODIGOS_TOKEN = {190}                                   # token inválido o vencido
CODIGOS_PERMISO = {10} | set(range(200, 300))           # faltan permisos
CODIGOS_LIMITE = {4, 17, 32, 613, 80001, 80002}         # límite de llamadas
CODIGOS_TEMPORALES = {1, 2}                             # error interno pasajero

PATRON_USERNAME = re.compile(r"^[a-z0-9._]{1,30}$")

log = logging.getLogger("extraccion")


class ErrorFatal(Exception):
    """Impide seguir con cualquier cuenta: token vencido, permisos, configuración o base de datos."""


class ErrorCuenta(Exception):
    """Propio de una cuenta (no existe, es personal, restricción de edad). Se registra y se sigue."""


def ahora_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def parsear_fecha(ts: str) -> datetime:
    """'2026-10-13T20:15:39+0000' -> datetime en UTC."""
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc)


def sin_zona(dt: datetime) -> datetime:
    """SQL Server (DATETIME2) recibe la fecha UTC sin zona horaria."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)


# --------------------------------------------------------------------------------------------
# Configuración
# --------------------------------------------------------------------------------------------

@dataclass
class Config:
    ig_user_id: str
    token: str
    version: str
    app_secret: str | None
    conexion_sql: str | None

    @classmethod
    def desde_entorno(cls) -> "Config":
        load_dotenv(RAIZ / ".env")
        faltan = [v for v in ("IG_USER_ID", "ACCESS_TOKEN") if not os.getenv(v, "").strip()]
        if faltan:
            raise ErrorFatal(f"Faltan variables en .env: {', '.join(faltan)}. Copia .env.example como .env y complétalo.")
        return cls(
            ig_user_id=os.environ["IG_USER_ID"].strip(),
            token=os.environ["ACCESS_TOKEN"].strip(),
            version=os.getenv("API_VERSION", "v26.0").strip(),
            app_secret=os.getenv("APP_SECRET", "").strip() or None,
            conexion_sql=os.getenv("SQL_CONEXION", "").strip() or None,
        )


# --------------------------------------------------------------------------------------------
# Cliente de la Graph API
# --------------------------------------------------------------------------------------------

class ClienteGraph:
    def __init__(self, config: Config, sesion: requests.Session | None = None,
                 dormir: Callable[[float], None] | None = None):
        self.config = config
        self.sesion = sesion or requests.Session()
        self.dormir = dormir or time.sleep
        self.base = f"https://graph.facebook.com/{config.version}"
        self.llamadas = 0

    def ocultar(self, texto: object) -> str:
        """Nunca deja el token ni el secret en logs o mensajes de error."""
        t = str(texto)
        for secreto in (self.config.token, self.config.app_secret):
            if secreto:
                t = t.replace(secreto, "***")
        return t

    def _parametros(self, campos: str) -> dict:
        params = {"fields": campos, "access_token": self.config.token}
        if self.config.app_secret:
            params["appsecret_proof"] = hmac.new(
                self.config.app_secret.encode(), self.config.token.encode(), hashlib.sha256
            ).hexdigest()
        return params

    def get(self, ruta: str, campos: str) -> dict:
        params = self._parametros(campos)
        intentos = esperas_limite = 0
        while True:
            if self.llamadas:
                self.dormir(PAUSA_ENTRE_LLAMADAS_S)
            self.llamadas += 1
            try:
                resp = self.sesion.get(f"{self.base}/{ruta}", params=params, timeout=30)
            except requests.RequestException as e:
                intentos += 1
                if intentos >= MAX_REINTENTOS:
                    raise ErrorCuenta(f"Error de red tras {intentos} intentos: {type(e).__name__}") from None
                log.warning("  Error de red (%s). Reintento %d/%d en %ds",
                            type(e).__name__, intentos, MAX_REINTENTOS, 10 * intentos)
                self.dormir(10 * intentos)
                continue

            try:
                cuerpo = resp.json()
            except ValueError:
                cuerpo = {}
            if resp.status_code == 200 and "error" not in cuerpo:
                self._pausar_si_uso_alto(resp.headers)
                return cuerpo

            error = cuerpo.get("error") or {}
            codigo, subcodigo = error.get("code"), error.get("error_subcode")
            mensaje = self.ocultar(error.get("error_user_msg") or error.get("message") or f"HTTP {resp.status_code}")

            if codigo in CODIGOS_TOKEN:
                raise ErrorFatal(f"El token no es válido o venció ({mensaje}). "
                                 "Genera uno nuevo en el Explorador de la API Graph y extiéndelo.")
            if codigo in CODIGOS_PERMISO:
                raise ErrorFatal(f"Faltan permisos en el token (código {codigo}): {mensaje}")
            if codigo in CODIGOS_LIMITE:
                esperas_limite += 1
                if esperas_limite > MAX_ESPERAS_POR_LIMITE:
                    raise ErrorFatal("Se alcanzó el límite de llamadas demasiadas veces seguidas. "
                                     "Espera un rato y vuelve a correr con --pendientes.")
                espera = self._segundos_para_recuperar(resp.headers)
                log.warning("  Límite de llamadas de la API (código %s). Esperando %d min…", codigo, espera // 60)
                self.dormir(espera)
                continue
            if codigo in CODIGOS_TEMPORALES or resp.status_code >= 500:
                intentos += 1
                if intentos >= MAX_REINTENTOS:
                    raise ErrorCuenta(f"Error temporal de la API tras {intentos} intentos: {mensaje}")
                log.warning("  Error temporal de la API (%s). Reintento %d/%d en %ds",
                            mensaje, intentos, MAX_REINTENTOS, 15 * intentos)
                self.dormir(15 * intentos)
                continue
            raise ErrorCuenta(f"[{codigo}/{subcodigo}] {mensaje}")

    @staticmethod
    def _uso(headers) -> tuple[int, int]:
        """(porcentaje de uso más alto, minutos estimados para recuperar acceso) según los headers."""
        maximo, minutos = 0, 0
        try:
            app = json.loads(headers.get("x-app-usage") or "{}")
            maximo = max([maximo] + [int(v) for v in app.values() if isinstance(v, (int, float))])
        except (ValueError, TypeError, AttributeError):
            pass
        try:
            for lista in json.loads(headers.get("x-business-use-case-usage") or "{}").values():
                for item in lista:
                    maximo = max(maximo, int(item.get("call_count", 0)),
                                 int(item.get("total_cputime", 0)), int(item.get("total_time", 0)))
                    minutos = max(minutos, int(item.get("estimated_time_to_regain_access", 0)))
        except (ValueError, TypeError, AttributeError):
            pass
        return maximo, minutos

    def _segundos_para_recuperar(self, headers) -> int:
        _, minutos = self._uso(headers)
        return minutos * 60 if minutos > 0 else ESPERA_LIMITE_POR_DEFECTO_S

    def _pausar_si_uso_alto(self, headers) -> None:
        uso, _ = self._uso(headers)
        if uso >= 90:
            espera = self._segundos_para_recuperar(headers)
            log.warning("  Uso de la API al %d%%. Pausa de %d min para no pasar el límite…", uso, espera // 60)
            self.dormir(espera)
        elif uso >= 75:
            log.info("  Uso de la API al %d%%. Pausa de 1 min…", uso)
            self.dormir(60)


# --------------------------------------------------------------------------------------------
# Extracción de una cuenta
# --------------------------------------------------------------------------------------------

@dataclass
class ResultadoCuenta:
    username: str
    ig_id: str | None
    seguidores: int | None
    cantidad_posts: int | None
    posts: list[dict]
    paginas_crudas: list[dict]


def extraer_cuenta(cliente: ClienteGraph, username: str, desde_utc: datetime) -> ResultadoCuenta:
    """Trae los posts de la cuenta desde el más reciente hasta pasar `desde_utc`."""
    base = f"business_discovery.username({username})"
    campos = f"{base}{{id,username,followers_count,media_count,media.limit({TAM_PAGINA}){{{CAMPOS_MEDIA}}}}}"
    respuesta = cliente.get(cliente.config.ig_user_id, campos)
    bd = respuesta.get("business_discovery")
    if not bd:
        raise ErrorCuenta("La respuesta no trae datos de business_discovery")

    paginas = [respuesta]
    posts: dict[str, dict] = {}
    media = bd.get("media") or {}
    pagina = 1
    while True:
        datos = [p for p in (media.get("data") or []) if p.get("id") and p.get("timestamp")]
        for p in datos:
            posts.setdefault(p["id"], p)
        cursor = ((media.get("paging") or {}).get("cursors") or {}).get("after")
        if not datos or not cursor:
            break
        if min(parsear_fecha(p["timestamp"]) for p in datos) < desde_utc:
            break
        if pagina >= MAX_PAGINAS_POR_CUENTA:
            log.warning("  %s: se alcanzó el tope de %d páginas", username, MAX_PAGINAS_POR_CUENTA)
            break
        pagina += 1
        log.info("  %s: página %d (%d posts hasta ahora)", username, pagina, len(posts))
        campos = f"{base}{{media.after({cursor}).limit({TAM_PAGINA}){{{CAMPOS_MEDIA}}}}}"
        respuesta = cliente.get(cliente.config.ig_user_id, campos)
        paginas.append(respuesta)
        media = ((respuesta.get("business_discovery") or {}).get("media")) or {}

    ordenados = sorted(posts.values(), key=lambda p: p["timestamp"], reverse=True)
    return ResultadoCuenta(
        username=username,
        ig_id=bd.get("id"),
        seguidores=bd.get("followers_count"),
        cantidad_posts=bd.get("media_count"),
        posts=ordenados,
        paginas_crudas=paginas,
    )


def fila_post(p: dict, username: str, captura: datetime) -> tuple:
    return (
        p["id"], username, p.get("caption"), sin_zona(parsear_fecha(p["timestamp"])),
        p.get("media_type"), p.get("media_product_type"), p.get("like_count"),
        p.get("comments_count"), p.get("permalink"), sin_zona(captura),
    )


# --------------------------------------------------------------------------------------------
# Almacenamiento
# --------------------------------------------------------------------------------------------

class Almacen(Protocol):
    def iniciar_ejecucion(self, parametros: str) -> int: ...
    def cuentas_ok_recientes(self, horas: int) -> set[str]: ...
    def guardar_resultado(self, cuenta: dict, res: ResultadoCuenta, captura: datetime, ejecucion_id: int) -> None: ...
    def guardar_error(self, cuenta: dict, mensaje: str, captura: datetime) -> None: ...
    def cerrar_ejecucion(self, ejecucion_id: int, ok: int, errores: int, posts: int) -> None: ...
    def cerrar(self) -> None: ...


class AlmacenSqlServer:
    TABLAS = ("Cuentas", "CuentasSnapshot", "Posts", "RespuestasCrudas", "Ejecuciones")

    def __init__(self, conexion: str):
        try:
            import pyodbc
        except ImportError:
            raise ErrorFatal("Falta pyodbc. Ejecuta: pip install -r requirements.txt") from None
        self._pyodbc = pyodbc
        try:
            self.cn = pyodbc.connect(conexion, autocommit=False)
        except pyodbc.Error as e:
            raise ErrorFatal(f"No se pudo conectar a SQL Server. Revisa SQL_CONEXION en .env. Detalle: {e}") from None
        cur = self.cn.cursor()
        faltan = [t for t in self.TABLAS
                  if cur.execute("SELECT OBJECT_ID(?, 'U')", f"dbo.{t}").fetchone()[0] is None]
        if faltan:
            raise ErrorFatal(f"Faltan tablas en la base ({', '.join(faltan)}). Ejecuta sql/01_esquema.sql en SSMS.")

    def _transaccion(self, trabajo: Callable) -> None:
        cur = self.cn.cursor()
        try:
            trabajo(cur)
            self.cn.commit()
        except self._pyodbc.Error as e:
            self.cn.rollback()
            raise ErrorFatal(f"Error al escribir en SQL Server: {e}") from None

    def iniciar_ejecucion(self, parametros: str) -> int:
        cur = self.cn.cursor()
        fila = cur.execute(
            "INSERT INTO dbo.Ejecuciones (InicioUtc, Parametros) OUTPUT INSERTED.EjecucionId VALUES (?, ?)",
            sin_zona(ahora_utc()), parametros[:500]).fetchone()
        self.cn.commit()
        return int(fila[0])

    def cuentas_ok_recientes(self, horas: int) -> set[str]:
        filas = self.cn.cursor().execute(
            "SELECT Username FROM dbo.Cuentas WHERE EstadoExtraccion = N'ok' "
            "AND UltimaExtraccionUtc >= DATEADD(HOUR, ?, SYSUTCDATETIME())", -horas).fetchall()
        return {f[0] for f in filas}

    @staticmethod
    def _upsert_cuenta_ok(cur, cuenta: dict, res: ResultadoCuenta, captura: datetime) -> None:
        datos = (cuenta.get("nombre"), cuenta.get("comuna"), cuenta.get("estado"), res.ig_id, res.seguidores,
                 res.cantidad_posts, len(res.posts), sin_zona(captura))
        cur.execute(
            """UPDATE dbo.Cuentas SET Nombre = ?, Comuna = ?, EstadoLista = ?, IgId = ?, Seguidores = ?,
                   CantidadPosts = ?, PostsExtraidos = ?, EstadoExtraccion = N'ok', MensajeError = NULL,
                   UltimaExtraccionUtc = ?
               WHERE Username = ?""", *datos, cuenta["username"])
        if cur.rowcount == 0:
            cur.execute(
                """INSERT INTO dbo.Cuentas (Nombre, Comuna, EstadoLista, IgId, Seguidores, CantidadPosts,
                       PostsExtraidos, UltimaExtraccionUtc, Username, EstadoExtraccion)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, N'ok')""", *datos, cuenta["username"])

    @staticmethod
    def _upsert_cuenta_error(cur, cuenta: dict, mensaje: str, captura: datetime) -> None:
        # Si la cuenta ya existía, conserva sus últimos seguidores conocidos
        datos = (cuenta.get("nombre"), cuenta.get("comuna"), cuenta.get("estado"), mensaje[:1000], sin_zona(captura))
        cur.execute(
            """UPDATE dbo.Cuentas SET Nombre = ?, Comuna = ?, EstadoLista = ?, PostsExtraidos = NULL,
                   EstadoExtraccion = N'error', MensajeError = ?, UltimaExtraccionUtc = ?
               WHERE Username = ?""", *datos, cuenta["username"])
        if cur.rowcount == 0:
            cur.execute(
                """INSERT INTO dbo.Cuentas (Nombre, Comuna, EstadoLista, MensajeError, UltimaExtraccionUtc,
                       Username, EstadoExtraccion)
                   VALUES (?, ?, ?, ?, ?, ?, N'error')""", *datos, cuenta["username"])

    def guardar_resultado(self, cuenta: dict, res: ResultadoCuenta, captura: datetime, ejecucion_id: int) -> None:
        def trabajo(cur):
            self._upsert_cuenta_ok(cur, cuenta, res, captura)
            if res.seguidores is not None and res.cantidad_posts is not None:
                cur.execute("INSERT INTO dbo.CuentasSnapshot (Username, FechaCapturaUtc, Seguidores, CantidadPosts) "
                            "VALUES (?, ?, ?, ?)", res.username, sin_zona(captura), res.seguidores, res.cantidad_posts)
            for f in (fila_post(p, res.username, captura) for p in res.posts):
                post_id, username, caption, publicado, tipo, producto, likes, comentarios, link, capturado = f
                cur.execute(
                    """UPDATE dbo.Posts SET Caption = ?, MediaType = ?, MediaProductType = ?, Likes = ?,
                           Comentarios = ?, Permalink = ?, FechaCapturaUtc = ?
                       WHERE PostId = ?""",
                    caption, tipo, producto, likes, comentarios, link, capturado, post_id)
                if cur.rowcount == 0:
                    cur.execute(
                        """INSERT INTO dbo.Posts (PostId, Username, Caption, FechaPublicacionUtc, MediaType,
                               MediaProductType, Likes, Comentarios, Permalink, FechaCapturaUtc)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", *f)
            for i, pagina in enumerate(res.paginas_crudas, 1):
                cur.execute("INSERT INTO dbo.RespuestasCrudas (EjecucionId, Username, Pagina, FechaCapturaUtc, Json) "
                            "VALUES (?, ?, ?, ?, ?)", ejecucion_id, res.username, i, sin_zona(captura),
                            json.dumps(pagina, ensure_ascii=False))
        self._transaccion(trabajo)

    def guardar_error(self, cuenta: dict, mensaje: str, captura: datetime) -> None:
        self._transaccion(lambda cur: self._upsert_cuenta_error(cur, cuenta, mensaje, captura))

    def cerrar_ejecucion(self, ejecucion_id: int, ok: int, errores: int, posts: int) -> None:
        self._transaccion(lambda cur: cur.execute(
            "UPDATE dbo.Ejecuciones SET FinUtc = ?, CuentasOk = ?, CuentasError = ?, PostsGuardados = ? "
            "WHERE EjecucionId = ?", sin_zona(ahora_utc()), ok, errores, posts, ejecucion_id))

    def cerrar(self) -> None:
        self.cn.close()


class AlmacenArchivos:
    """Para --probar: guarda en salida_prueba/<fecha>/ en vez de SQL Server."""

    def __init__(self, carpeta: Path):
        self.carpeta = carpeta
        (carpeta / "crudos").mkdir(parents=True, exist_ok=True)

    def _agregar(self, archivo: str, fila: dict) -> None:
        with open(self.carpeta / archivo, "a", encoding="utf-8") as f:
            f.write(json.dumps(fila, ensure_ascii=False, default=str) + "\n")

    def iniciar_ejecucion(self, parametros: str) -> int:
        self._agregar("ejecucion.jsonl", {"inicio_utc": ahora_utc(), "parametros": parametros})
        return 0

    def cuentas_ok_recientes(self, horas: int) -> set[str]:
        return set()

    def guardar_resultado(self, cuenta: dict, res: ResultadoCuenta, captura: datetime, ejecucion_id: int) -> None:
        self._agregar("cuentas.jsonl", {**cuenta, "estado_extraccion": "ok", "ig_id": res.ig_id,
                                        "seguidores": res.seguidores, "cantidad_posts": res.cantidad_posts,
                                        "posts_extraidos": len(res.posts), "captura_utc": captura})
        columnas = ("post_id", "username", "caption", "publicacion_utc", "media_type", "media_product_type",
                    "likes", "comentarios", "permalink", "captura_utc")
        for p in res.posts:
            self._agregar("posts.jsonl", dict(zip(columnas, fila_post(p, res.username, captura))))
        for i, pagina in enumerate(res.paginas_crudas, 1):
            (self.carpeta / "crudos" / f"{res.username}_p{i}.json").write_text(
                json.dumps(pagina, ensure_ascii=False, indent=2), encoding="utf-8")

    def guardar_error(self, cuenta: dict, mensaje: str, captura: datetime) -> None:
        self._agregar("cuentas.jsonl", {**cuenta, "estado_extraccion": "error", "mensaje": mensaje,
                                        "captura_utc": captura})

    def cerrar_ejecucion(self, ejecucion_id: int, ok: int, errores: int, posts: int) -> None:
        self._agregar("ejecucion.jsonl", {"fin_utc": ahora_utc(), "ok": ok, "errores": errores, "posts": posts})

    def cerrar(self) -> None:
        pass


# --------------------------------------------------------------------------------------------
# Lista de cuentas
# --------------------------------------------------------------------------------------------

def leer_lista(ruta: Path, sin_dudosas: bool = False) -> list[dict]:
    if not ruta.exists():
        raise ErrorFatal(f"No se encontró la lista de cuentas: {ruta}")
    with open(ruta, encoding="utf-8-sig", newline="") as f:
        lector = csv.DictReader(f)
        if not lector.fieldnames or "username" not in lector.fieldnames:
            raise ErrorFatal(f"El CSV no tiene la columna 'username': {ruta}")
        filas = list(lector)

    cuentas, vistos = [], set()
    for fila in filas:
        username = (fila.get("username") or "").strip().lstrip("@").lower()
        if not username or username in vistos:
            continue
        if not PATRON_USERNAME.match(username):
            log.warning("Username con formato inválido, se omite: %r", username)
            continue
        if sin_dudosas and (fila.get("estado") or "").strip() == "dudoso":
            continue
        vistos.add(username)
        cuentas.append({"username": username, "nombre": fila.get("nombre"),
                        "comuna": fila.get("comuna"), "estado": fila.get("estado")})
    return cuentas


# --------------------------------------------------------------------------------------------
# Ejecución
# --------------------------------------------------------------------------------------------

@dataclass
class Resumen:
    ok: int = 0
    errores: int = 0
    saltadas: int = 0
    posts: int = 0
    abortado: str | None = None


def ejecutar(cliente: ClienteGraph, almacen: Almacen, cuentas: list[dict], desde_utc: datetime,
             parametros: str, saltar: set[str] | None = None) -> tuple[Resumen, list[tuple[str, str]]]:
    resumen, errores = Resumen(), []
    saltar = saltar or set()
    ejecucion_id = almacen.iniciar_ejecucion(parametros)
    try:
        for i, cuenta in enumerate(cuentas, 1):
            username = cuenta["username"]
            if username in saltar:
                resumen.saltadas += 1
                log.info("[%d/%d] %s: ya extraída hace poco, se salta", i, len(cuentas), username)
                continue
            captura = ahora_utc()
            try:
                res = extraer_cuenta(cliente, username, desde_utc)
            except (ErrorCuenta, KeyError, TypeError, ValueError) as e:
                # KeyError/TypeError/ValueError: respuesta con una forma inesperada; no detiene las demás cuentas
                mensaje = cliente.ocultar(e if isinstance(e, ErrorCuenta) else f"Respuesta inesperada: {e!r}")
                resumen.errores += 1
                errores.append((username, mensaje))
                almacen.guardar_error(cuenta, mensaje, captura)
                log.warning("[%d/%d] %s: ERROR — %s", i, len(cuentas), username, mensaje)
                continue
            almacen.guardar_resultado(cuenta, res, captura, ejecucion_id)
            resumen.ok += 1
            resumen.posts += len(res.posts)
            rango = (f"{res.posts[-1]['timestamp'][:10]} → {res.posts[0]['timestamp'][:10]}"
                     if res.posts else "sin posts")
            log.info("[%d/%d] %s: %d posts (%s), %s seguidores",
                     i, len(cuentas), username, len(res.posts), rango, res.seguidores)
    except ErrorFatal as e:
        resumen.abortado = str(e)
        log.error("Se detuvo la extracción: %s", e)
    except KeyboardInterrupt:
        resumen.abortado = "interrumpido por el usuario"
        log.warning("Interrumpido. Lo ya guardado queda en la base; corre con --pendientes para continuar.")
    finally:
        try:
            almacen.cerrar_ejecucion(ejecucion_id, resumen.ok, resumen.errores, resumen.posts)
        except ErrorFatal as e:
            log.error("No se pudo cerrar el registro de la ejecución: %s", e)
    return resumen, errores


def configurar_log() -> Path:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # consola de Windows y salida redirigida
    carpeta = RAIZ / "logs"
    carpeta.mkdir(exist_ok=True)
    archivo = carpeta / f"extraccion_{datetime.now():%Y%m%d_%H%M%S}.log"
    formato = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    consola = logging.StreamHandler(sys.stdout)
    consola.setFormatter(formato)
    en_archivo = logging.FileHandler(archivo, encoding="utf-8")
    en_archivo.setFormatter(formato)
    log.handlers[:] = [consola, en_archivo]
    log.setLevel(logging.INFO)
    return archivo


def parsear_argumentos(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Extrae posts públicos de Instagram (Business Discovery) a SQL Server.")
    p.add_argument("--csv", default=str(RAIZ / "data" / "barberias_santiago.csv"), help="Lista de cuentas")
    p.add_argument("--usuarios", help="Solo estos usernames, separados por coma")
    p.add_argument("--meses", type=int, default=12, help="Meses de historia por cuenta (por defecto 12)")
    p.add_argument("--sin-dudosas", action="store_true", help="Omite las cuentas marcadas como 'dudoso' en el CSV")
    p.add_argument("--pendientes", action="store_true", help="Salta las cuentas extraídas OK en las últimas horas")
    p.add_argument("--horas", type=int, default=24, help="Ventana para --pendientes (por defecto 24)")
    p.add_argument("--probar", action="store_true",
                   help="Prueba con 1 cuenta (o las de --usuarios) y guarda en archivos, sin SQL Server")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parsear_argumentos(argv)
    archivo_log = configurar_log()
    inicio = time.monotonic()
    try:
        config = Config.desde_entorno()
        cuentas = leer_lista(Path(args.csv), args.sin_dudosas)
        if args.usuarios:
            pedidos = {u.strip().lstrip("@").lower() for u in args.usuarios.split(",") if u.strip()}
            no_estan = pedidos - {c["username"] for c in cuentas}
            cuentas = [c for c in cuentas if c["username"] in pedidos]
            cuentas += [{"username": u, "nombre": None, "comuna": None, "estado": None}
                        for u in sorted(no_estan) if PATRON_USERNAME.match(u)]
        elif args.probar:
            cuentas = cuentas[:1]
        if not cuentas:
            raise ErrorFatal("No hay cuentas para extraer.")

        cliente = ClienteGraph(config)
        try:
            yo = cliente.get(config.ig_user_id, "username")
        except ErrorCuenta as e:
            raise ErrorFatal(f"No se pudo leer tu propia cuenta ({config.ig_user_id}): {e}. "
                             "Revisa IG_USER_ID en .env.") from None
        log.info("Conectado como @%s — API %s", yo.get("username"), config.version)

        if args.probar:
            carpeta = RAIZ / "salida_prueba" / f"{datetime.now():%Y%m%d_%H%M%S}"
            almacen: Almacen = AlmacenArchivos(carpeta)
            log.info("Modo prueba: se guarda en %s", carpeta)
        else:
            if not config.conexion_sql:
                raise ErrorFatal("Falta SQL_CONEXION en .env.")
            almacen = AlmacenSqlServer(config.conexion_sql)
    except ErrorFatal as e:
        log.error("%s", e)
        return 2

    desde = ahora_utc() - timedelta(days=round(30.44 * args.meses))
    saltar = almacen.cuentas_ok_recientes(args.horas) if args.pendientes else set()
    parametros = (f"cuentas={len(cuentas)} meses={args.meses} sin_dudosas={args.sin_dudosas} "
                  f"pendientes={args.pendientes} probar={args.probar}")
    log.info("Extrayendo %d cuentas, posts desde %s (UTC)", len(cuentas), desde.date())

    resumen, errores = ejecutar(cliente, almacen, cuentas, desde, parametros, saltar)
    almacen.cerrar()

    minutos = (time.monotonic() - inicio) / 60
    log.info("-" * 60)
    log.info("Cuentas OK: %d | con error: %d | saltadas: %d | posts guardados: %d | %d llamadas | %.1f min",
             resumen.ok, resumen.errores, resumen.saltadas, resumen.posts, cliente.llamadas, minutos)
    for username, mensaje in errores:
        log.info("  ✗ %s: %s", username, mensaje)
    if resumen.abortado:
        log.info("Quedó incompleta (%s). Vuelve a correr con --pendientes para seguir donde quedó.", resumen.abortado)
    log.info("Log completo en %s", archivo_log)
    return 1 if resumen.abortado else 0


if __name__ == "__main__":
    sys.exit(main())
