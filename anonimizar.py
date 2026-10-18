"""
analisis-barberias-peluquerias — nombres anónimos para publicar resultados.

Reemplaza cada username por "Barbería 01", "Barbería 02", ... ordenadas de la cuenta con más posts a la con menos
(empates por orden alfabético). El mismo archivo de datos da siempre los mismos códigos, así que notebooks y
dashboard muestran las mismas etiquetas.
"""
from __future__ import annotations

import pandas as pd


def mapa_cuentas(usuarios: pd.Series) -> dict[str, str]:
    conteo = usuarios.value_counts()
    orden = sorted(conteo.index, key=lambda u: (-conteo[u], u))
    ancho = max(2, len(str(len(orden))))
    return {u: f"Barbería {i:0{ancho}d}" for i, u in enumerate(orden, 1)}


def anonimizar(df: pd.DataFrame, columna: str = "username", mapa: dict[str, str] | None = None) -> pd.DataFrame:
    """Copia de df con la columna de usuario reemplazada y sin textos que puedan nombrar a personas."""
    mapa = mapa or mapa_cuentas(df[columna])
    salida = df.copy()
    salida[columna] = salida[columna].map(mapa)
    return salida.drop(columns=[c for c in ("texto_propio", "caption", "permalink") if c in salida.columns])
