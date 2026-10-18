"""
analisis-barberias-peluquerias — Fase 4
Regresión lineal implementada a mano con descenso de gradiente.

Minimiza el error cuadrático medio (con pesos opcionales) más una penalización L2 opcional:

    J(β, b) = [ Σ wᵢ (yᵢ − xᵢ·β − b)²  +  α ‖β‖² ] / Σ wᵢ

    ∂J/∂β = [ −2 Xᵀ (w ∘ r)  +  2 α β ] / Σ wᵢ        donde r = y − Xβ − b
    ∂J/∂b =   −2 Σ wᵢ rᵢ / Σ wᵢ

Con α = 0 llega a la misma solución que sklearn.linear_model.LinearRegression,
y con α > 0 a la de sklearn.linear_model.Ridge(alpha=α). El intercepto no se penaliza.

Es compatible con scikit-learn (fit / predict / get_params), así que se puede usar dentro de un Pipeline.
Conviene estandarizar las features antes (StandardScaler): con escalas muy distintas el descenso
de gradiente converge lentísimo o diverge.
"""
from __future__ import annotations

import numpy as np
from sklearn.base import BaseEstimator, RegressorMixin
from sklearn.utils.validation import check_is_fitted


class RegresionLinealGD(RegressorMixin, BaseEstimator):
    """
    Parámetros
    ----------
    tasa_aprendizaje : tamaño del paso en cada iteración.
    max_iteraciones  : tope de iteraciones.
    alpha            : penalización L2 (0 = regresión lineal sin regularizar).
    tolerancia       : se detiene cuando la pérdida mejora menos que esto entre dos iteraciones.

    Atributos después de fit
    ------------------------
    coef_, intercept_   : coeficientes aprendidos.
    historial_perdida_  : valor de J en cada iteración (para graficar la convergencia).
    n_iter_             : iteraciones usadas.
    convergio_          : True si se detuvo por tolerancia y no por el tope.
    """

    def __init__(self, tasa_aprendizaje: float = 0.1, max_iteraciones: int = 50_000,
                 alpha: float = 0.0, tolerancia: float = 1e-12):
        self.tasa_aprendizaje = tasa_aprendizaje
        self.max_iteraciones = max_iteraciones
        self.alpha = alpha
        self.tolerancia = tolerancia

    def fit(self, X, y, sample_weight=None):
        X = np.asarray(X, dtype=float)
        y = np.asarray(y, dtype=float).ravel()
        if X.ndim != 2 or len(X) != len(y):
            raise ValueError("X debe ser una matriz 2D con tantas filas como elementos tiene y")
        w = np.ones(len(y)) if sample_weight is None else np.asarray(sample_weight, dtype=float).ravel()
        if np.any(w < 0) or w.sum() <= 0:
            raise ValueError("Los pesos deben ser no negativos y sumar más que 0")
        suma_w = w.sum()

        beta = np.zeros(X.shape[1])
        b = np.average(y, weights=w)            # partir desde el promedio acelera la convergencia
        historial = []
        convergio = False
        for i in range(self.max_iteraciones):
            r = y - (X @ beta + b)
            perdida = (np.sum(w * r ** 2) + self.alpha * beta @ beta) / suma_w
            if not np.isfinite(perdida):
                raise FloatingPointError("La pérdida diverge: baja la tasa de aprendizaje o estandariza las features")
            historial.append(perdida)
            if i > 0:
                mejora = historial[-2] - perdida
                if mejora < -1e-9 * max(1.0, historial[-2]):
                    raise FloatingPointError("La pérdida aumentó: la tasa de aprendizaje es demasiado alta "
                                             "(o las features no están estandarizadas)")
                if mejora < self.tolerancia:
                    convergio = True
                    break
            grad_beta = (-2 * X.T @ (w * r) + 2 * self.alpha * beta) / suma_w
            grad_b = -2 * np.sum(w * r) / suma_w
            beta -= self.tasa_aprendizaje * grad_beta
            b -= self.tasa_aprendizaje * grad_b

        self.coef_ = beta
        self.intercept_ = float(b)
        self.historial_perdida_ = np.array(historial)
        self.n_iter_ = len(historial)
        self.convergio_ = convergio
        self.n_features_in_ = X.shape[1]
        return self

    def predict(self, X):
        check_is_fitted(self, "coef_")
        return np.asarray(X, dtype=float) @ self.coef_ + self.intercept_
