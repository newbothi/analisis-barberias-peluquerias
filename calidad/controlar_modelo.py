"""
analisis-barberias-peluquerias — control de calidad del modelo

Antes de que entrenar_modelo.py reemplace modelos/modelo_final.*, el modelo nuevo tiene que pasar estos chequeos:

  1. supera_referencia    tiene menos error (MAE) que la referencia simple: el promedio de los últimos posts de la
                          cuenta. Si no le gana, el modelo no aporta nada y no vale la pena usarlo.
  2. explica_variacion    R² > 0 en la validación temporal: predice mejor que un valor constante.
  3. no_empeora           su MAE no es más de un 5% peor que el del modelo guardado anteriormente. Si los datos
                          cambiaron (nueva extracción), un cambio puede ser esperable: se revisa y, si está bien, se
                          acepta a mano con --aceptar-modelo.
  4. predicciones_validas todas sus predicciones son números finitos y en un rango posible de log_likes.
  5. coeficientes_finitos si es un modelo lineal, ningún coeficiente es infinito o vacío.

Si alguno falla, entrenar_modelo.py no toca el modelo guardado y deja el candidato en
modelos/candidato_rechazado.json con el motivo.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

TOLERANCIA_EMPEORAR = 0.05
LOG_MIN, LOG_MAX = -1.0, float(np.log1p(10_000_000))      # de ~0 a 10 millones de likes


@dataclass
class Chequeo:
    nombre: str
    aprobado: bool
    detalle: str


@dataclass
class ControlModelo:
    chequeos: list[Chequeo]

    @property
    def aprobado(self) -> bool:
        return all(c.aprobado for c in self.chequeos)

    @property
    def fallidos(self) -> list[Chequeo]:
        return [c for c in self.chequeos if not c.aprobado]

    def como_lista(self) -> list[dict]:
        return [asdict(c) for c in self.chequeos]


def controlar_modelo(metricas: dict, referencia: dict, predicciones: np.ndarray, anterior: dict | None = None,
                     coeficientes: np.ndarray | None = None, tolerancia: float = TOLERANCIA_EMPEORAR) -> ControlModelo:
    """
    metricas, referencia: métricas de validación temporal del modelo y de la referencia (al menos MAE y R2).
    predicciones:         predicciones del modelo final en log_likes.
    anterior:             contenido de modelos/modelo_final.json del modelo guardado antes, si existe.
    """
    chequeos = []
    mae, mae_ref = float(metricas["MAE"]), float(referencia["MAE"])
    chequeos.append(Chequeo(
        "supera_referencia", mae < mae_ref,
        f"MAE {mae:.3f} vs {mae_ref:.3f} de la referencia ({(1 - mae / mae_ref) * 100:+.1f}%)"))

    r2 = float(metricas["R2"])
    chequeos.append(Chequeo("explica_variacion", r2 > 0, f"R² = {r2:.3f}"))

    if anterior and "metricas_validacion_temporal" in anterior:
        mae_ant = float(anterior["metricas_validacion_temporal"]["MAE"])
        limite = mae_ant * (1 + tolerancia)
        cambio = (mae / mae_ant - 1) * 100
        datos = ""
        if anterior.get("entrenado_con_posts") is not None:
            datos = f"; el anterior se entrenó con {anterior['entrenado_con_posts']} posts"
        chequeos.append(Chequeo(
            "no_empeora", mae <= limite,
            f"MAE {mae:.3f} vs {mae_ant:.3f} del modelo anterior ({cambio:+.1f}%, máximo +{tolerancia:.0%}){datos}"))
    else:
        chequeos.append(Chequeo("no_empeora", True, "no hay modelo anterior con qué comparar"))

    pred = np.asarray(predicciones, dtype=float)
    finitas = np.isfinite(pred)
    en_rango = finitas & (pred >= LOG_MIN) & (pred <= LOG_MAX)
    chequeos.append(Chequeo(
        "predicciones_validas", bool(en_rango.all()) and len(pred) > 0,
        f"{len(pred)} predicciones entre {np.nanmin(pred):.2f} y {np.nanmax(pred):.2f}" if en_rango.all() and len(pred)
        else f"{int((~finitas).sum())} no finitas y {int((finitas & ~en_rango).sum())} fuera de rango"))

    if coeficientes is not None:
        coef = np.asarray(coeficientes, dtype=float)
        malos = int((~np.isfinite(coef)).sum())
        chequeos.append(Chequeo("coeficientes_finitos", malos == 0,
                                f"{len(coef)} coeficientes finitos" if malos == 0 else f"{malos} coeficientes no finitos"))
    return ControlModelo(chequeos)
