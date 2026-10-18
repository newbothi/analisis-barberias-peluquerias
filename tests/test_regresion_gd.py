"""
Pruebas de la regresión lineal con descenso de gradiente propio.
Ejecutar desde la raíz del proyecto:  python -m pytest -q
"""
import sys
from pathlib import Path

import numpy as np
import pytest
from sklearn.base import clone
from sklearn.linear_model import LinearRegression, Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from regresion_gd import RegresionLinealGD  # noqa: E402


@pytest.fixture
def datos():
    rng = np.random.default_rng(7)
    X = rng.normal(size=(300, 5))
    X[:, 4] = X[:, 3] * 0.8 + rng.normal(scale=0.3, size=300)        # dos features correlacionadas
    y = 2.0 + X @ np.array([1.5, -0.7, 0.0, 0.4, 0.9]) + rng.normal(scale=0.5, size=300)
    return StandardScaler().fit_transform(X), y


def test_igual_a_linear_regression(datos):
    X, y = datos
    gd = RegresionLinealGD().fit(X, y)
    sk = LinearRegression().fit(X, y)
    assert gd.convergio_
    np.testing.assert_allclose(gd.coef_, sk.coef_, atol=1e-4)
    assert gd.intercept_ == pytest.approx(sk.intercept_, abs=1e-4)


def test_igual_a_ridge_con_pesos(datos):
    X, y = datos
    w = np.random.default_rng(1).uniform(0.2, 3.0, len(y))
    gd = RegresionLinealGD(alpha=25.0).fit(X, y, sample_weight=w)
    sk = Ridge(alpha=25.0).fit(X, y, sample_weight=w)
    np.testing.assert_allclose(gd.coef_, sk.coef_, atol=1e-4)
    assert gd.intercept_ == pytest.approx(sk.intercept_, abs=1e-4)


def test_la_perdida_baja_en_cada_iteracion(datos):
    X, y = datos
    historial = RegresionLinealGD().fit(X, y).historial_perdida_
    assert len(historial) > 10
    assert np.all(np.diff(historial) <= 1e-12)


def test_tasa_demasiado_alta_diverge(datos):
    X, y = datos
    with pytest.raises(FloatingPointError):
        RegresionLinealGD(tasa_aprendizaje=5.0, max_iteraciones=5000).fit(X, y)


def test_tope_de_iteraciones():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(50, 2))
    gd = RegresionLinealGD(max_iteraciones=3).fit(X, X @ [1.0, 2.0])
    assert gd.n_iter_ == 3 and not gd.convergio_


def test_predecir_sin_entrenar_falla():
    from sklearn.exceptions import NotFittedError
    with pytest.raises(NotFittedError):
        RegresionLinealGD().predict(np.zeros((2, 2)))


def test_dentro_de_un_pipeline_con_pesos(datos):
    X, y = datos
    w = np.ones(len(y))
    pipe = Pipeline([("escalar", StandardScaler()), ("modelo", RegresionLinealGD())])
    pipe.fit(X, y, modelo__sample_weight=w)
    copia = clone(pipe)
    assert copia.get_params()["modelo__tasa_aprendizaje"] == 0.1
    assert np.corrcoef(pipe.predict(X), y)[0, 1] > 0.9
