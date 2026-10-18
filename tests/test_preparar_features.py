"""
Pruebas de la Fase 2 sin SQL Server.
Ejecutar desde la raíz del proyecto:  python -m pytest -q
"""
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import preparar_features as pf  # noqa: E402

CAPTURA = datetime(2026, 10, 14, 22, 0, tzinfo=timezone.utc)
PLANTILLA = "Somos Barbería Ejemplo 💈\nAv. Siempre Viva 742, Ñuñoa\nWsp: +56 9 1111 1111\n#barberia\n#santiago"


def post(username, dias_atras, likes=100, caption=None, media="VIDEO", producto="REELS", n=0, seguidores=5000,
         comentarios=5, estado="encontrado"):
    return {
        "post_id": f"{username}-{n}", "username": username,
        "caption": caption if caption is not None else f"Corte número {n}\n{PLANTILLA}",
        "publicacion_utc": CAPTURA - timedelta(days=dias_atras), "media_type": media, "media_product_type": producto,
        "likes": likes, "comentarios": comentarios, "captura_utc": CAPTURA, "seguidores": seguidores,
        "estado_lista": estado,
    }


def cuenta(username, cantidad, cada_dias=3.0, desde=8, **kw):
    return [post(username, desde + i * cada_dias, n=i, likes=50 + i, **kw) for i in range(cantidad)]


# ------------------------------------------------------------------ plantilla

def test_detecta_y_quita_la_plantilla():
    captions = [f"Corte {i}\n{PLANTILLA}" for i in range(20)]
    plantilla = pf.detectar_plantilla(captions)
    assert "wsp: +56 9 1111 1111" in plantilla and "#barberia" in plantilla
    assert pf.quitar_plantilla(captions[3], plantilla) == "Corte 3"


def test_no_quita_nada_con_pocos_posts():
    assert pf.detectar_plantilla([f"Corte {i}\n{PLANTILLA}" for i in range(5)]) == set()


def test_linea_repetida_pocas_veces_no_es_plantilla():
    captions = [f"Post {i}" for i in range(40)] + ["Agenda tu hora"] * 3
    assert "agenda tu hora" not in pf.detectar_plantilla(captions)


def test_plantilla_ignora_mayusculas_y_espacios():
    captions = [f"Post {i}\n  SOMOS   la barbería " for i in range(12)] + [f"x {i}\nsomos la Barbería" for i in range(3)]
    plantilla = pf.detectar_plantilla(captions)
    assert pf.quitar_plantilla("Nuevo corte\nSomos LA barbería", plantilla) == "Nuevo corte"


# ------------------------------------------------------------------ conversiones

def test_hora_de_chile_y_dia():
    datos = pd.DataFrame(cuenta("a", 25))
    datos.loc[0, "publicacion_utc"] = datetime(2026, 10, 6, 20, 15, 39, tzinfo=timezone.utc)   # martes 17:15 en Chile
    res = pf.construir_features(datos)
    fila = res.posts[res.posts["post_id"] == "a-0"].iloc[0]
    assert fila["hora_local"] == 17 and fila["nombre_dia"] == "martes" and fila["franja"] == "tarde"
    assert not fila["es_fin_de_semana"]


@pytest.mark.parametrize("media,producto,esperado", [
    ("VIDEO", "REELS", "reel"), ("CAROUSEL_ALBUM", "FEED", "carrusel"),
    ("IMAGE", "FEED", "imagen"), ("VIDEO", "FEED", "video_feed"), (None, None, "otro"),
])
def test_formato(media, producto, esperado):
    assert pf.formato(media, producto) == esperado


@pytest.mark.parametrize("hora,esperado", [(0, "madrugada"), (5, "madrugada"), (6, "manana"),
                                           (11, "manana"), (12, "tarde"), (17, "tarde"), (18, "noche"), (23, "noche")])
def test_franja(hora, esperado):
    assert pf.franja(hora) == esperado


# ------------------------------------------------------------------ filtros y embudo

def test_embudo_y_filtros():
    filas = cuenta("a", 25)
    filas.append(post("a", 2, n=100))                                   # muy reciente
    filas.append(post("a", 500, n=101))                                 # fuera de la ventana
    filas.append(post("a", 20, n=102, likes=None))                      # likes ocultos
    filas.append(post("a", 21, n=103, caption="¡SORTEO! participa y gana"))
    res = pf.construir_features(pd.DataFrame(filas))
    pasos = dict(res.embudo)
    assert pasos["Posts extraídos"] == 29
    assert pasos["Publicados en los últimos 12 meses"] == 28
    assert pasos["Con al menos 7 días de antigüedad"] == 27
    assert pasos["Con likes visibles"] == 26
    assert pasos["Sin sorteos ni concursos"] == 25
    assert len(res.posts) == 25
    c = res.cuentas.iloc[0]
    assert (c["posts_muy_recientes"], c["posts_sin_likes"], c["sorteos"], c["posts_validos"]) == (1, 1, 1, 25)


def test_cuentas_con_pocos_posts_o_inactivas_se_excluyen():
    filas = cuenta("activa", 25) + cuenta("chica", 5) + cuenta("inactiva", 30, desde=400)
    res = pf.construir_features(pd.DataFrame(filas))
    motivos = res.cuentas.set_index("username")["motivo_exclusion"]
    assert pd.isna(motivos["activa"])
    assert "5 posts válidos" in motivos["chica"]
    assert "sin posts" in motivos["inactiva"]
    assert set(res.posts["username"]) == {"activa"}


def test_cuenta_sin_seguidores_se_excluye():
    res = pf.construir_features(pd.DataFrame(cuenta("x", 25, seguidores=None)))
    assert res.posts.empty and res.cuentas.iloc[0]["motivo_exclusion"] == "sin dato de seguidores"


# ------------------------------------------------------------------ features

def test_ritmo_de_publicacion():
    res = pf.construir_features(pd.DataFrame(cuenta("a", 25, cada_dias=2.0)))
    p = res.posts.sort_values("fecha_publicacion_utc")
    assert np.isnan(p.iloc[0]["dias_desde_post_anterior"])
    assert p.iloc[1:]["dias_desde_post_anterior"].round(3).eq(2.0).all()
    assert p.iloc[0]["posts_ultimos_7_dias"] == 0
    assert p.iloc[-1]["posts_ultimos_7_dias"] == 3        # posts a 2, 4 y 6 días antes


def test_features_de_texto():
    filas = cuenta("a", 25)
    filas[0]["caption"] = f"¿Qué corte prefieres? 🔥🔥 agenda por DM #fade\n{PLANTILLA}"
    filas[1]["caption"] = f"Promo martes $8.000\n{PLANTILLA}"
    res = pf.construir_features(pd.DataFrame(filas)).posts.set_index("post_id")
    a0, a1, a2 = res.loc["a-0"], res.loc["a-1"], res.loc["a-2"]
    assert a0["tiene_pregunta"] and a0["tiene_llamado_accion"] and a0["emojis_propios"] == 2
    assert a0["hashtags_propios"] == 1 and a0["hashtags_total"] == 3
    assert a1["menciona_precio_promo"] and not a1["tiene_pregunta"]
    assert a2["texto_propio"] == "Corte número 2" and not a2["tiene_llamado_accion"]   # el "Wsp" de la plantilla no cuenta


def test_target_y_peso():
    filas = cuenta("grande", 60) + cuenta("chica", 20)
    res = pf.construir_features(pd.DataFrame(filas)).posts
    assert np.allclose(res["log_likes"], np.log1p(res["likes"]))
    assert res["peso_cuenta"].mean() == pytest.approx(1.0)
    sumas = res.groupby("username")["peso_cuenta"].sum()
    assert sumas["grande"] == pytest.approx(sumas["chica"])              # cada cuenta pesa lo mismo
    assert (res["engagement_rate"] > 0).all()


def test_columnas_de_salida_coinciden_con_sql():
    esquema = (Path(__file__).resolve().parents[1] / "sql" / "03_features.sql").read_text(encoding="utf-8")
    for nombre in list(pf.COLUMNAS_SALIDA.values()) + list(pf.COLUMNAS_CUENTAS.values()):
        assert re.search(rf"^\s+{nombre}\s", esquema, re.M), f"{nombre} no está en 03_features.sql"


def test_filas_para_sql_son_tipos_nativos():
    res = pf.construir_features(pd.DataFrame(cuenta("a", 25)))
    filas = pf.filas_para_sql(res.posts, list(pf.COLUMNAS_SALIDA)) + pf.filas_para_sql(res.cuentas, list(pf.COLUMNAS_CUENTAS))
    permitidos = (type(None), str, int, float, bool, datetime)
    for fila in filas:
        for v in fila:
            assert isinstance(v, permitidos), f"{type(v)}: {v!r}"
    assert all(len(f) == len(pf.COLUMNAS_SALIDA) for f in filas[:len(res.posts)])
