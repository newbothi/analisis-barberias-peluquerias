# Detalle técnico por fase

Documentación de cada script: qué hace, cómo se usa, qué archivos escribe y qué decisiones se tomaron. El resumen del
proyecto y los resultados están en el [README](../README.md).

- [Fase 1: extracción de datos](#fase-1-extracción-de-datos)
- [Fase 2: limpieza y features](#fase-2-limpieza-y-features)
- [Fase 3: exploración](#fase-3-exploración)
- [Fase 4: modelo](#fase-4-modelo)
- [Fase 5: interpretación y presentación](#fase-5-interpretación-y-presentación)
- [Pruebas](#pruebas)

Los controles de calidad (validación de datos, control del modelo, escaneo de privacidad e integración continua) se
explican en [`CALIDAD.md`](CALIDAD.md).

---

## Fase 1: extracción de datos

`extraer_instagram.py` lee la lista de cuentas (`data/barberias_santiago.csv`; el repositorio trae un ejemplo en
`data/barberias_ejemplo.csv`), consulta
**Business Discovery** de la API de Instagram para cada username y guarda en SQL Server los datos
públicos de cada cuenta y sus posts de los últimos 12 meses.

Business Discovery solo entrega datos públicos de cuentas profesionales (empresa o creador):
seguidores, cantidad de posts y, por post, caption, fecha, formato, likes, comentarios y link.
No entrega alcance, guardados ni datos demográficos.

### Requisitos

- Python 3.10 o superior
- SQL Server (probado con la instancia local por defecto, autenticación de Windows)
- **ODBC Driver 18 for SQL Server**. Para ver qué drivers tienes, en PowerShell:
  ```powershell
  Get-OdbcDriver -Name "*SQL Server*" | Select-Object Name
  ```
  Si solo aparece el 17, cambia el nombre del driver en `SQL_CONEXION`. Si no aparece ninguno, instálalo desde la página de Microsoft.
- Una app de Meta for Developers con un token que tenga estos permisos:
  `pages_show_list`, `pages_read_engagement`, `instagram_basic`, `instagram_manage_insights`,
  `business_management`, `ads_read`

### Instalación (PowerShell)

```powershell
cd analisis-barberias-peluquerias
python -m venv .venv
.venv\Scripts\Activate.ps1          # en cmd: .venv\Scripts\activate.bat
pip install -r requirements.txt
copy .env.example .env               # luego completa .env
```

### Configuración

1. **Base de datos:** abre `sql/01_esquema.sql` en SSMS y ejecútalo. Crea la base
   `AnalisisBarberiasPeluquerias` y sus tablas. Se puede ejecutar de nuevo sin riesgo.
2. **`.env`:** completa `IG_USER_ID`, `ACCESS_TOKEN` (el token extendido de ~60 días) y revisa `SQL_CONEXION`.
   `APP_SECRET` es opcional; si lo pones, cada llamada va firmada.

### Uso

```powershell
# 1) Prueba con una cuenta, sin tocar SQL Server (guarda en salida_prueba\)
python extraer_instagram.py --probar --usuarios nombre.de.una.cuenta

# 2) Corrida completa: todas las cuentas del CSV que tienen username
python extraer_instagram.py

# 3) Si se cortó (límite de la API, token, Ctrl+C), continúa saltando las que ya salieron bien
python extraer_instagram.py --pendientes
```

Otras opciones:

| Opción | Qué hace |
|---|---|
| `--usuarios a,b` | Solo esas cuentas (pueden no estar en el CSV) |
| `--meses 6` | Meses de historia por cuenta (por defecto 12) |
| `--sin-dudosas` | Omite las cuentas marcadas `dudoso` en el CSV |
| `--pendientes --horas 48` | Cambia la ventana de lo que se considera "ya extraído" |

Cada corrida deja un log en `logs\` y un resumen en la tabla `Ejecuciones`.

### Qué hace el script

- Valida el token leyendo tu propia cuenta antes de empezar.
- Pide los posts en páginas de 50 y sigue el cursor `after` hasta pasar la fecha de corte.
- Si una cuenta falla (no existe, es personal, tiene restricción de edad), guarda el motivo en
  `Cuentas.MensajeError` y sigue con la siguiente.
- Si el token venció o faltan permisos, se detiene con un mensaje claro.
- Hace una pausa de 2 s entre llamadas y lee los headers de uso de la API; si se acerca al límite
  o lo alcanza, espera lo que indica la API y continúa.
- Guarda cada cuenta en una transacción: si se corta a la mitad, lo ya guardado queda bien.
- Si un post ya existe, actualiza likes y comentarios (la fecha de captura permite calcular la edad del post).
- El token nunca se escribe en el log.

### Tablas

| Tabla | Contenido |
|---|---|
| `Cuentas` | Una fila por cuenta: datos del CSV, seguidores, estado de la última extracción y error si hubo |
| `CuentasSnapshot` | Seguidores en cada corrida (sirve para ver crecimiento) |
| `Posts` | Un post por fila: caption, fecha UTC, formato, likes, comentarios, link y fecha de captura |
| `RespuestasCrudas` | El JSON original de cada página, por si después se necesita un campo |
| `Ejecuciones` | Resumen de cada corrida |

Las fechas se guardan en **UTC**. La conversión a hora de Chile se hace en la Fase 2.

### Revisar el resultado

`sql/02_revision.sql` tiene consultas para ver las cuentas con error, la cobertura por cuenta
(posts, primer y último post, días sin publicar) y las candidatas a descartar en la Fase 2.

### Errores comunes

| Mensaje | Qué hacer |
|---|---|
| `El token no es válido o venció` | Genera un token nuevo en el Explorador de la API Graph y extiéndelo en el Access Token Debugger |
| `Faltan permisos en el token` | Regenera el token con los 6 permisos de la lista de requisitos |
| `No se pudo conectar a SQL Server` | Revisa el driver y el servidor en `SQL_CONEXION` |
| `Faltan tablas en la base` | Ejecuta `sql/01_esquema.sql` en SSMS |
| Cuenta con error `[110/...]` o `[100/...]` | El username no existe, cambió o es una cuenta personal: corrígelo en el CSV |

---

## Fase 2: limpieza y features

`preparar_features.py` toma los posts de la Fase 1, los limpia y calcula las features y el target del modelo.

### Uso

```powershell
pip install -r requirements.txt       # agrega pandas y tzdata
# Ejecutar una vez en SSMS: sql/03_features.sql
python preparar_features.py
```

Guarda el resultado en `dbo.PostsFeatures` y `dbo.CuentasResumen` (se vacían y se vuelven a llenar en
cada corrida) y en `data/posts_features.csv` y `data/cuentas_resumen.csv` para la Fase 3.

Antes de guardar, valida la tabla con `calidad/validar_datos.py` (24 reglas, ver [CALIDAD.md](CALIDAD.md)). Si alguna
regla da error, no guarda nada, termina con código 3 y el detalle queda en `reportes/calidad/`.

| Opción | Qué hace |
|---|---|
| `--min-posts 15` | Posts válidos mínimos para que una cuenta entre al dataset (por defecto 20) |
| `--min-edad-dias 7` | Antigüedad mínima del post al leer sus likes (por defecto 7) |
| `--meses 12` | Ventana de posts a considerar (por defecto 12) |
| `--sin-sql` | Lee de SQL Server pero solo escribe los CSV |
| `--desde-jsonl salida_prueba\...` | Prueba con la salida de `extraer_instagram.py --probar`, sin SQL Server |

### Limpieza (embudo)

Cada paso se informa en el log con cuántos posts quedan:

1. **Ventana:** posts de los últimos 12 meses.
2. **Antigüedad:** al menos 7 días entre la publicación y la lectura de likes. Un post de ayer todavía está sumando.
3. **Likes visibles:** se descartan los posts de cuentas que ocultan el contador.
4. **Sin sorteos:** captions con *sorteo, concurso, giveaway, participa, ganador*. Inflan likes y comentarios.
5. **Cuentas con al menos 20 posts válidos.** Las demás quedan en `CuentasResumen` con el motivo.

### Plantilla del caption

Muchas cuentas repiten en cada post un mismo bloque (dirección, WhatsApp, equipo, hashtags fijos).
Si no se quita, el largo del caption y la cantidad de hashtags miden la plantilla y no el post.
Una línea se considera plantilla si la cuenta la repite en **al menos el 10% de sus posts y en 5 o más**.
Las features de texto se calculan sobre el **texto propio** (el caption sin esas líneas).

### Features

| Grupo | Columnas |
|---|---|
| Tiempo (hora de Chile) | `fecha_local`, `anio`, `mes`, `dia_semana` (0 = lunes), `nombre_dia`, `es_fin_de_semana`, `hora_local`, `franja` (madrugada 0-5, mañana 6-11, tarde 12-17, noche 18-23) |
| Formato | `formato`: reel, carrusel, imagen, video_feed |
| Texto propio | `largo_texto_propio`, `palabras_texto_propio`, `hashtags_propios`, `hashtags_total`, `emojis_propios`, `tiene_pregunta`, `tiene_llamado_accion`, `menciona_precio_promo` |
| Ritmo de la cuenta | `dias_desde_post_anterior`, `posts_ultimos_7_dias` |
| Tamaño de la cuenta | `seguidores`, `log_seguidores` |
| Control | `edad_dias_captura` (días entre publicación y lectura de likes) |
| Target | `likes`, `log_likes` = ln(1 + likes) |
| Otros | `comentarios`, `log_comentarios`, `engagement_rate`, `peso_cuenta` |

`peso_cuenta` hace que cada cuenta pese lo mismo en total, aunque una tenga 400 posts y otra 25
(promedio = 1). En la Fase 4 se probó como `sample_weight`; no mejoró de forma consistente y el modelo final va sin pesos.

### Limitaciones conocidas

- **Seguidores actuales:** la API solo da los seguidores de hoy, no los que tenía la cuenta al publicar cada post.
  Con una ventana de 12 meses el error es acotado.
- **Edad del post:** los reels siguen sumando likes después de la primera semana. `edad_dias_captura` queda como
  variable de control.
- **Sin estadísticas del target por cuenta:** el promedio de likes de cada cuenta no se calcula aquí, porque
  usarlo con todos los datos filtra información del conjunto de prueba. Se calcula en la Fase 4, solo con el
  conjunto de entrenamiento.

---

## Fase 3: exploración

`notebooks/03_exploracion.ipynb` lee `data/posts_features.csv` y responde dónde hay señal antes de modelar.

### Cómo abrirlo

- **Solo para verlo:** GitHub muestra el notebook con sus gráficos, sin instalar nada.
- **Para ejecutarlo:** en VS Code, con las extensiones *Python* y *Jupyter*, eligiendo como kernel el `.venv` del proyecto.

Viene ejecutado con los datos del 14-10-2026; *Run All* lo recalcula con los datos actuales. Con `ANONIMIZAR = True`
(por defecto) las cuentas aparecen como "Barbería 01..19" y no se muestran textos de los posts.

### Método

Comparar todos los reels contra todas las imágenes mezcla el formato con la cuenta. Por eso cada efecto se mide
**dentro de cada cuenta** (diferencia de `log_likes` promedio entre dos grupos de una misma cuenta) y se resume con:
en cuántas cuentas va a favor, la mediana y un intervalo de confianza del 90% por **bootstrap de cuentas**.

### Hallazgos principales

| Hallazgo | Detalle |
|---|---|
| Likes muy concentrados | mediana 23; el 1% de los posts concentra el 80% de los likes. Los virales son humor y contingencia |
| La cuenta pesa mucho | explica ~41% de la variación; el doble de seguidores ≈ +60% de likes, no el doble |
| Carrusel > imagen | ~+60% dentro de la cuenta, en 6 de 7 cuentas (señal clara) |
| Llamado a la acción | ~−25% (agenda, reserva, WhatsApp), en 10 de 16 cuentas (señal clara) |
| Reels | +39% de mediana, pero no concluyente |
| Día, hora, preguntas, ritmo, largo del texto | sin señal clara |

El notebook cierra con las decisiones para la Fase 4 (features, target, validación y manejo de virales).

---

## Fase 4: modelo

`entrenar_modelo.py` entrena y compara modelos que predicen `log_likes` = ln(1 + likes), y guarda el mejor.
`regresion_gd.py` es una regresión lineal implementada desde cero con descenso de gradiente.
`notebooks/04_modelo.ipynb` muestra los resultados con gráficos.

### Uso

```powershell
pip install -r requirements.txt       # agrega scikit-learn
python entrenar_modelo.py
```

Lee `data/posts_features.csv` (no necesita SQL Server) y tarda unos segundos. Escribe:

| Archivo | Contenido |
|---|---|
| `data/resultados_modelos.csv` | Métricas de cada modelo en los dos escenarios |
| `data/predicciones_validacion.csv` | Predicciones fuera de muestra (escenario A), base del dashboard de la Fase 5 |
| `data/coeficientes_modelo.csv` | Coeficientes del modelo final en escala original y su efecto en % |
| `modelos/modelo_final.joblib` | Modelo final entrenado con todos los posts |
| `modelos/modelo_final.json` | Features, métricas, parámetros y versiones del modelo final |

Opciones: `--ventana 20` (posts anteriores para el nivel reciente), `--cortes 4` (validación temporal),
`--folds-cuenta 5` (validación por cuenta), `--aceptar-modelo` (guardar aunque falle el control de calidad).

Control de calidad: antes de entrenar valida los datos (código 3 si fallan). Antes de reemplazar el modelo guardado,
`calidad/controlar_modelo.py` comprueba que el nuevo le gane a la referencia, tenga R² > 0, no empeore más de un 5%
y dé predicciones válidas. Si no pasa, mantiene el modelo anterior, deja `modelos/candidato_rechazado.json` y termina
con código 4 (detalle en [CALIDAD.md](CALIDAD.md)).

### Dos escenarios

| Escenario | Pregunta | Validación |
|---|---|---|
| A. Barbería conocida | ¿Cuántos likes tendrá el próximo post de una cuenta con historial? | Temporal: entrena con el pasado, evalúa los posts siguientes (4 cortes, 646 posts evaluados) |
| B. Barbería nueva | ¿Y si la cuenta nunca se vio? | GroupKFold por cuenta |

En el escenario A se usa la feature `nivel_reciente`: el promedio de log_likes de los **20 posts anteriores** de la misma
cuenta (solo pasado, sin fuga de información). Una prueba automática verifica que cambiar los likes futuros no cambia
ninguna predicción anterior.

### Resultados (datos del 14-10-2026)

| Escenario A | MAE (log) | Error típico | R² |
|---|---|---|---|
| **Huber (lineal robusta) + nivel** — modelo final | **0,687** | **×1,99** | **0,44** |
| Nivel reciente (referencia) | 0,697 | ×2,01 | 0,43 |
| Regresión lineal + nivel (scikit-learn y descenso de gradiente propio) | 0,708 | ×2,03 | 0,43 |
| Random Forest + nivel | 0,729 | ×2,07 | 0,37 |
| Promedio histórico de la cuenta (referencia) | 0,760 | ×2,14 | 0,35 |
| Mediana global (referencia) | 0,964 | ×2,62 | −0,04 |

- El **nivel reciente de la cuenta** es lo que más predice; las features del post mejoran poco (~1% por post, ~4% por cuenta).
- **Huber** le gana a la regresión lineal clásica porque los virales distorsionan el error cuadrático.
- En el **escenario B** ningún modelo le gana a la mediana global: sin historial de la cuenta, el error típico es cercano a ×3.
- La **regresión con descenso de gradiente propio** llega a los mismos coeficientes que scikit-learn (diferencia < 1e-5).

### Decisiones probadas

| Decisión | Resultado |
|---|---|
| Pesos por cuenta (`peso_cuenta`) | No mejoran de forma consistente; se entrena sin pesos |
| Ventana del nivel reciente | 20 posts funciona bien; 10 queda parecido |
| Error cuadrático vs robusto | Huber (robusto) es mejor con estos datos |

## Fase 5: interpretación y presentación

### Interpretación

`interpretar_modelo.py` traduce el modelo final a efectos en % sobre los likes, con intervalos de confianza, y a
recomendaciones. `notebooks/05_interpretacion.ipynb` lo muestra con gráficos.

```powershell
python interpretar_modelo.py                    # ~20 s; --repeticiones 1000 para más precisión
```

| Archivo | Contenido |
|---|---|
| `data/interpretacion_coeficientes.csv` | Efecto de cada variable (%), IC 90%, % de signo estable, conclusión y recomendación |
| `data/efectos_dentro_de_cuentas.csv` | Comparaciones simples de la Fase 3 (dentro de cada cuenta) con IC 90% |

**Método.** Los intervalos salen de un **bootstrap de cuentas**: se sortean 19 cuentas con reposición, se reentrena el
modelo y se repite 500 veces. Se remuestrean cuentas y no posts porque los posts de una cuenta no son independientes.
Una variable solo genera recomendación si es una decisión del post y su intervalo no cruza el 0. El notebook agrega dos
chequeos: que el modelo coincida con las comparaciones simples de la Fase 3 y que ningún efecto cambie de signo al sacar
una cuenta a la vez.

### Dashboard de Power BI

```powershell
python preparar_dashboard.py                    # tablas anonimizadas en powerbi/datos/
```

Crea 7 tablas (`posts`, `cuentas`, `embudo`, `efectos`, `efectos_simples`, `modelos`, `predicciones`) con las cuentas como
"Barbería 01..19" y sin textos de posts. `powerbi/GUIA_DASHBOARD.md` explica paso a paso cómo armar las 4 páginas,
con las relaciones y las medidas DAX, y `powerbi/tema.json` trae la paleta de colores. `--con-nombres` deja los
usernames reales, solo para uso personal.

El `.pbix` y `powerbi/datos/` no se suben a GitHub (están en `.gitignore`): el `.pbix` guarda una copia de los datos.
Para mostrar el dashboard se usan capturas en `docs/img/`.

### Anonimización

`anonimizar.py` asigna a cada cuenta un código estable ("Barbería 01" = la que tiene más posts). Lo usan los tres
notebooks y el dashboard, así que una misma barbería tiene el mismo código en todas partes.

---

## Pruebas

```powershell
python -m pytest -q
```

Son 115 pruebas. Simulan la API y SQL Server, y en GitHub corren solas en cada push.
- **Fase 1:** paginación, corte por fecha, errores por cuenta, token vencido, límite de llamadas y que cada
  sentencia SQL reciba los parámetros correctos.
- **Fase 2:** detección de plantilla, hora de Chile, filtros del embudo, exclusión de cuentas, ritmo de
  publicación, features de texto, peso por cuenta y que las columnas coincidan con `03_features.sql`.
- **Fase 4:** la regresión con descenso de gradiente coincide con `LinearRegression` y `Ridge` (con pesos), detecta
  divergencia; el nivel reciente usa solo el pasado; la validación temporal no mira el futuro; el modelo recupera
  efectos conocidos en datos sintéticos; el script escribe todos sus archivos.
- **Fase 5:** el bootstrap trata como distintas las cuentas repetidas, es reproducible y recupera los efectos de datos
  sintéticos (y "sin evidencia" donde no hay efecto); la anonimización es estable y quita los textos; las tablas del
  dashboard no llevan usernames ni captions y sus ids calzan entre tablas.
- **Calidad:** cada regla de la validación detecta el problema que le corresponde (ids repetidos, likes negativos,
  franja incoherente con la hora, fechas fuera de la ventana, etc.) y la salida real de `preparar_features.py` la
  pasa; `preparar_features.py` no guarda y `entrenar_modelo.py` no entrena con datos inválidos; el control del modelo
  rechaza cada caso malo y `entrenar_modelo.py` mantiene el modelo anterior salvo con `--aceptar-modelo`.
- **Privacidad:** `.gitignore` cubre los archivos sensibles y lo publicable no contiene tokens, credenciales locales,
  usernames ni nombres reales; los notebooks tienen `ANONIMIZAR = True`.
