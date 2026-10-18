"""
Pruebas de la Fase 1 sin llamar a la API real ni a SQL Server.
Ejecutar desde la raíz del proyecto:  python -m pytest -q
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from requests.structures import CaseInsensitiveDict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import extraer_instagram as ext  # noqa: E402

TOKEN = "EAAtokenSecretoDePrueba123"


# ------------------------------------------------------------------ dobles de prueba

class Respuesta:
    def __init__(self, cuerpo, status=200, headers=None):
        self._cuerpo, self.status_code = cuerpo, status
        self.headers = CaseInsensitiveDict(headers or {})

    def json(self):
        return self._cuerpo


class SesionFalsa:
    """Responde según una función que recibe el parámetro 'fields'."""

    def __init__(self, responder):
        self.responder, self.pedidos = responder, []

    def get(self, url, params=None, timeout=None):
        self.pedidos.append(params["fields"])
        return self.responder(params["fields"])


def config():
    return ext.Config(ig_user_id="17840000000000000", token=TOKEN, version="v26.0", app_secret=None, conexion_sql=None)


def cliente(responder, dormidas=None):
    dormidas = dormidas if dormidas is not None else []
    return ext.ClienteGraph(config(), SesionFalsa(responder), dormir=dormidas.append), dormidas


def posts(desde_dias, cantidad, cada_dias=2, prefijo="p"):
    hoy = datetime.now(timezone.utc)
    return [{
        "id": f"{prefijo}{desde_dias + i}",
        "caption": "Corte del día\n#barberia",
        "timestamp": (hoy - timedelta(days=desde_dias + i * cada_dias)).strftime("%Y-%m-%dT%H:%M:%S+0000"),
        "media_type": "VIDEO", "media_product_type": "REELS",
        "like_count": 100 + i, "comments_count": i, "permalink": "https://www.instagram.com/reel/x/",
    } for i in range(cantidad)]


def pagina(datos, after=None, primera=True):
    media = {"data": datos}
    if after:
        media["paging"] = {"cursors": {"after": after}}
    bd = {"media": media, "id": "1784cuenta"}
    if primera:
        bd.update(followers_count=1234, media_count=900)
    return {"business_discovery": bd, "id": "17840000000000000"}


def error(codigo, mensaje="x", subcodigo=None, status=400, headers=None):
    return Respuesta({"error": {"code": codigo, "message": mensaje, "error_subcode": subcodigo}}, status, headers)


# ------------------------------------------------------------------ extracción y paginación

def test_pagina_hasta_pasar_la_fecha_de_corte():
    p1, p2, p3 = posts(0, 50), posts(100, 50), posts(200, 50)

    def responder(campos):
        if "media.after(B)" in campos:
            return Respuesta(pagina(p3, after="C", primera=False))
        if "media.after(A)" in campos:
            return Respuesta(pagina(p2, after="B", primera=False))
        return Respuesta(pagina(p1, after="A"))

    c, _ = cliente(responder)
    desde = datetime.now(timezone.utc) - timedelta(days=150)
    res = ext.extraer_cuenta(c, "barberia.uno", desde)

    assert c.llamadas == 2                      # la página 2 ya pasa los 150 días: no pide la 3
    assert len(res.posts) == 100
    assert res.seguidores == 1234 and res.cantidad_posts == 900
    assert len(res.paginas_crudas) == 2
    assert res.posts[0]["timestamp"] > res.posts[-1]["timestamp"]   # ordenados del más nuevo al más antiguo


def test_sin_cursor_termina_en_la_primera_pagina():
    c, _ = cliente(lambda campos: Respuesta(pagina(posts(0, 7))))
    res = ext.extraer_cuenta(c, "cuenta", datetime.now(timezone.utc) - timedelta(days=365))
    assert c.llamadas == 1 and len(res.posts) == 7


def test_cuenta_sin_posts():
    c, _ = cliente(lambda campos: Respuesta({"business_discovery": {"followers_count": 10, "media_count": 0, "id": "1"}}))
    res = ext.extraer_cuenta(c, "vacia", datetime.now(timezone.utc) - timedelta(days=365))
    assert res.posts == [] and res.seguidores == 10


def test_posts_repetidos_entre_paginas_no_se_duplican():
    p1 = posts(0, 50)

    def responder(campos):
        if "media.after(A)" in campos:
            return Respuesta(pagina(p1[-5:] + posts(100, 10, prefijo="q"), primera=False))
        return Respuesta(pagina(p1, after="A"))

    c, _ = cliente(responder)
    res = ext.extraer_cuenta(c, "cuenta", datetime.now(timezone.utc) - timedelta(days=365))
    assert len(res.posts) == 60


def test_post_sin_like_count_queda_como_none():
    datos = posts(0, 1)
    del datos[0]["like_count"]
    c, _ = cliente(lambda campos: Respuesta(pagina(datos)))
    res = ext.extraer_cuenta(c, "oculta", datetime.now(timezone.utc) - timedelta(days=30))
    fila = ext.fila_post(res.posts[0], "oculta", ext.ahora_utc())
    assert fila[6] is None


def test_los_campos_piden_todo_lo_necesario():
    c, _ = cliente(lambda campos: Respuesta(pagina(posts(0, 1))))
    ext.extraer_cuenta(c, "barberia.uno", datetime.now(timezone.utc))
    pedido = c.sesion.pedidos[0]
    assert pedido.startswith("business_discovery.username(barberia.uno){")
    for campo in ("followers_count", "media_count", "caption", "timestamp", "media_type",
                  "media_product_type", "like_count", "comments_count", "permalink"):
        assert campo in pedido
    assert pedido.count("{") == pedido.count("}")


# ------------------------------------------------------------------ errores y límites

def test_error_de_cuenta_no_detiene_el_resto(tmp_path):
    def responder(campos):
        if "noexiste" in campos:
            return error(110, "Invalid user id", 2207013)
        return Respuesta(pagina(posts(0, 3)))

    c, _ = cliente(responder)
    almacen = ext.AlmacenArchivos(tmp_path)
    cuentas = [{"username": "noexiste"}, {"username": "barberia.dos"}]
    resumen, errores = ext.ejecutar(c, almacen, cuentas, datetime.now(timezone.utc) - timedelta(days=30), "test")

    assert resumen.ok == 1 and resumen.errores == 1 and resumen.posts == 3 and resumen.abortado is None
    assert errores[0][0] == "noexiste" and "110" in errores[0][1]
    estados = [json.loads(l)["estado_extraccion"] for l in (tmp_path / "cuentas.jsonl").read_text(encoding="utf-8").splitlines()]
    assert estados == ["error", "ok"]


def test_token_vencido_detiene_todo(tmp_path):
    c, _ = cliente(lambda campos: error(190, "Error validating access token"))
    cuentas = [{"username": "a"}, {"username": "b"}]
    resumen, _ = ext.ejecutar(c, ext.AlmacenArchivos(tmp_path), cuentas, datetime.now(timezone.utc), "test")
    assert resumen.abortado and "token" in resumen.abortado.lower()
    assert c.llamadas == 1


def test_permiso_faltante_es_fatal():
    c, _ = cliente(lambda campos: error(10, "Application does not have permission for this action"))
    with pytest.raises(ext.ErrorFatal):
        c.get("x", "y")


def test_limite_de_llamadas_espera_y_reintenta():
    respuestas = iter([
        error(4, "Application request limit reached", headers={
            "x-business-use-case-usage": json.dumps({"1": [{"type": "instagram", "call_count": 100,
                                                             "estimated_time_to_regain_access": 2}]})}),
        Respuesta(pagina(posts(0, 1))),
    ])
    c, dormidas = cliente(lambda campos: next(respuestas))
    assert "business_discovery" in c.get("x", "y")
    assert 120 in dormidas


def test_uso_alto_hace_pausa_preventiva():
    headers = {"x-app-usage": json.dumps({"call_count": 92, "total_cputime": 10, "total_time": 12})}
    c, dormidas = cliente(lambda campos: Respuesta(pagina(posts(0, 1)), headers=headers))
    c.get("x", "y")
    assert ext.ESPERA_LIMITE_POR_DEFECTO_S in dormidas


def test_error_temporal_se_reintenta():
    respuestas = iter([error(2, "Service temporarily unavailable", status=500), Respuesta(pagina(posts(0, 1)))])
    c, dormidas = cliente(lambda campos: next(respuestas))
    c.get("x", "y")
    assert c.llamadas == 2 and 15 in dormidas


def test_el_token_nunca_aparece_en_mensajes():
    c, _ = cliente(lambda campos: error(100, f"Invalid parameter access_token={TOKEN}"))
    with pytest.raises(ext.ErrorCuenta) as e:
        c.get("x", "y")
    assert TOKEN not in str(c.ocultar(e.value))
    assert "***" in c.ocultar(e.value)


def test_appsecret_proof_se_agrega_si_hay_secret():
    cfg = config()
    cfg.app_secret = "secreto"
    params = ext.ClienteGraph(cfg, SesionFalsa(lambda c: None))._parametros("username")
    assert re.fullmatch(r"[0-9a-f]{64}", params["appsecret_proof"])


# ------------------------------------------------------------------ lista y conversión

def test_leer_lista(tmp_path):
    csv = tmp_path / "lista.csv"
    csv.write_text(
        "﻿nombre,comuna,direccion,username,estado,nota,fuente\n"
        "A,Ñuñoa,,@Barberia.Uno,encontrado,,\n"
        "B,Ñuñoa,,barberia.uno,encontrado,,\n"       # duplicado
        "C,Providencia,,,no_encontrado,,\n"               # sin username
        "D,Las Condes,,barberia.tres,dudoso,,\n"
        "E,Centro,,mal}nombre,encontrado,,\n",            # formato inválido
        encoding="utf-8")
    assert [c["username"] for c in ext.leer_lista(csv)] == ["barberia.uno", "barberia.tres"]
    assert [c["username"] for c in ext.leer_lista(csv, sin_dudosas=True)] == ["barberia.uno"]


def test_lista_de_ejemplo_del_proyecto():
    cuentas = ext.leer_lista(Path(__file__).resolve().parents[1] / "data" / "barberias_ejemplo.csv")
    assert [c["username"] for c in cuentas] == ["barberia.ejemplo", "otra.barberia"]


def test_fechas_quedan_en_utc_sin_zona():
    assert ext.sin_zona(ext.parsear_fecha("2026-10-13T20:15:39+0000")) == datetime(2026, 10, 13, 20, 15, 39)


# ------------------------------------------------------------------ SQL Server (sin servidor)

class CursorFalso:
    """Verifica que cada sentencia reciba tantos parámetros como '?' tiene."""

    def __init__(self, sentencias):
        self.sentencias, self.rowcount = sentencias, 0

    def execute(self, sql, *params):
        assert sql.count("?") == len(params), f"{sql.count('?')} '?' pero {len(params)} parámetros:\n{sql}"
        self.sentencias.append(sql.split()[0].upper() + " " + re.search(r"dbo\.\w+", sql).group(0))
        self.rowcount = 0   # simula que nada existe: los UPDATE no afectan filas y se hace INSERT
        return self

    def fetchone(self):
        return (1,)

    def fetchall(self):
        return []


class ConexionFalsa:
    def __init__(self):
        self.sentencias, self.commits = [], 0

    def cursor(self):
        return CursorFalso(self.sentencias)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def almacen_sql_falso():
    alm = ext.AlmacenSqlServer.__new__(ext.AlmacenSqlServer)
    alm.cn = ConexionFalsa()
    alm._pyodbc = type("pyodbc", (), {"Error": Exception})
    return alm


def test_sentencias_sql_tienen_parametros_correctos():
    alm = almacen_sql_falso()
    c, _ = cliente(lambda campos: Respuesta(pagina(posts(0, 3))))
    res = ext.extraer_cuenta(c, "barberia.uno", datetime.now(timezone.utc) - timedelta(days=30))
    cuenta = {"username": "barberia.uno", "nombre": "Barbería Uno", "comuna": "Ñuñoa", "estado": "encontrado"}
    captura = ext.ahora_utc()

    eid = alm.iniciar_ejecucion("test")
    alm.guardar_resultado(cuenta, res, captura, eid)
    alm.guardar_error({"username": "otra"}, "fallo", captura)
    alm.cuentas_ok_recientes(24)
    alm.cerrar_ejecucion(eid, 1, 1, 3)

    s = alm.cn.sentencias
    assert s.count("INSERT dbo.Posts") == 3
    assert "INSERT dbo.Cuentas" in s and "INSERT dbo.CuentasSnapshot" in s and "INSERT dbo.RespuestasCrudas" in s
    assert s.index("INSERT dbo.Cuentas") < s.index("INSERT dbo.Posts")   # la FK exige la cuenta antes que los posts
    assert alm.cn.commits == 4
