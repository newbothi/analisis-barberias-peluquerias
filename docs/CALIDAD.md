# Calidad: QA y QC

El proyecto separa dos tipos de controles:

- **QA (aseguramiento de calidad):** procesos que evitan que entren errores al código o se publique algo que no
  corresponde. Corren en cada `git push`.
- **QC (control de calidad):** chequeos sobre los datos y el modelo que se ejecutan en cada corrida del pipeline. Si
  algo no cumple, el script se detiene antes de guardar.

| Tipo | Control | Dónde | Cuándo corre | Si falla |
|---|---|---|---|---|
| QA | Pruebas automáticas | `tests/` | `python -m pytest -q` y en cada push (GitHub Actions) | El badge del README queda en rojo |
| QA | Escaneo de privacidad | `tests/test_privacidad.py` | Junto con las pruebas | La prueba indica el archivo y la línea, sin mostrar el secreto |
| QC | Validación de datos | `calidad/validar_datos.py` | En `preparar_features.py` antes de guardar y en `entrenar_modelo.py` antes de entrenar | No guarda ni entrena (código 3) y deja un reporte |
| QC | Control del modelo | `calidad/controlar_modelo.py` | En `entrenar_modelo.py` antes de guardar el modelo final | No reemplaza el modelo guardado (código 4) |

## QA · Integración continua

`.github/workflows/pruebas.yml` instala el proyecto en una máquina Linux limpia con Python 3.13 y corre todas las
pruebas en cada push y pull request. Las pruebas usan datos sintéticos, una API simulada y un SQL Server simulado, así
que no necesitan credenciales ni datos reales. El resultado se ve en la pestaña **Actions** del repositorio y en el
badge del README.

## QA · Escaneo de privacidad

`tests/test_privacidad.py` revisa lo que subiría `git add .`, es decir, los archivos versionados más los nuevos que
`.gitignore` no excluye:

| Prueba | Qué revisa | Dónde corre |
|---|---|---|
| `.gitignore` cubre lo sensible | `.env`, datos fila a fila, logs, respuestas de la API, reportes, datos de Power BI y `.pbix` | Siempre |
| Nada sensible se publica | Que ninguno de esos archivos esté en la lista de publicables | Siempre |
| Sin secretos | Patrones de token de Meta, `ACCESS_TOKEN`/`APP_SECRET` con valor y contraseñas en cadenas de conexión | Siempre |
| Tus credenciales | Que los valores reales de tu `.env` no aparezcan en ningún archivo, incluidos los binarios | Solo en tu PC |
| Cuentas reales | Que no aparezcan usernames, nombres de barberías ni direcciones de tus datos locales, ni tu usuario personal de Instagram (sacado de los logs) | Solo en tu PC |
| Notebooks anonimizados | Que los notebooks publicados tengan `ANONIMIZAR = True` | Siempre |

En los notebooks se revisa el código, el markdown y las salidas de texto; las imágenes se excluyen. Las pruebas que
necesitan datos locales se omiten en GitHub Actions, donde esos archivos no existen.

## QC · Validación de datos

`calidad/validar_datos.py` revisa `posts_features` con 24 reglas. Cada regla termina como **OK**, **ERROR** (los datos
no se usan), **AVISO** (conviene revisar, pero no bloquea) u **OMITIDA** (faltan las columnas que necesita).

| Grupo | Reglas | Severidad |
|---|---|---|
| Estructura | Columnas obligatorias presentes, sin vacíos donde no corresponde, tabla no vacía | Error |
| Identidad | `post_id` único, cada post con cuenta | Error |
| Fechas | Válidas y no en el futuro; dentro de la ventana de meses; antigüedad mínima al leer los likes | Error |
| Target | Likes enteros y ≥ 0; `log_likes` = ln(1 + likes) | Error |
| Cuenta | Seguidores > 0 y coherentes con `log_seguidores`; peso por cuenta con promedio 1; mínimo de posts por cuenta | Error |
| Tiempo | Hora entre 0 y 23; día, nombre del día y fin de semana coherentes; franja coherente con la hora | Error |
| Texto | Conteos enteros y ≥ 0; hashtags propios ≤ hashtags totales; columnas verdadero/falso válidas | Error |
| Otros | Días desde el post anterior ≥ 0; engagement finito y ≥ 0; comentarios ≥ 0 | Error |
| Composición | Formato reconocido; comentarios sin dato; al menos 10 cuentas; ninguna cuenta con más del 40% de los posts | Aviso |

Cada corrida deja un reporte en `reportes/calidad/validacion_<origen>_<fecha>.md`, con el estado de cada regla y
ejemplos de los `post_id` afectados. La carpeta no se sube a GitHub, porque los reportes pueden identificar posts.

También se puede correr sola:

```powershell
python -m calidad.validar_datos --min-posts 10
```

## QC · Control del modelo

`calidad/controlar_modelo.py` decide si el modelo recién entrenado puede reemplazar al guardado en `modelos/`:

| Chequeo | Condición |
|---|---|
| `supera_referencia` | Menor MAE que la referencia simple (promedio de los últimos 20 posts de la cuenta) |
| `explica_variacion` | R² > 0 en la validación temporal |
| `no_empeora` | MAE no más de un 5% peor que el del modelo guardado antes |
| `predicciones_validas` | Todas las predicciones son finitas y están en un rango posible de log_likes |
| `coeficientes_finitos` | Ningún coeficiente infinito o vacío (modelos lineales) |

Si pasa, el resultado de cada chequeo queda registrado en `modelos/modelo_final.json` (`control_calidad`). Si no
pasa, el modelo guardado no se toca y el candidato queda en `modelos/candidato_rechazado.json` con el motivo.

Cuando el cambio es esperable, por ejemplo después de extraer datos nuevos, se revisa y se acepta a mano:

```powershell
python entrenar_modelo.py --aceptar-modelo
```

El JSON del modelo queda marcado con `"aceptado_manualmente": true`.

## Códigos de salida

| Código | Significado |
|---|---|
| 0 | Todo bien |
| 2 | Error de ejecución: falta un archivo, la configuración o la conexión |
| 3 | Los datos no pasaron la validación: no se guardó ni se entrenó nada |
| 4 | El modelo no pasó el control: se mantiene el modelo anterior |
