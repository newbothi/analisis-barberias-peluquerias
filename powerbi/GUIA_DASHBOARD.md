# Guía del dashboard de Power BI

El dashboard tiene 4 páginas: **Resumen del nicho**, **Qué funciona**, **Modelo** y **Barberías**. Los datos vienen de
`preparar_dashboard.py` y el tema de colores de `tema.json`, la misma paleta de los notebooks.

> **Privacidad.** El archivo `.pbix` guarda una copia de todos los datos importados, así que **no se sube a GitHub**:
> `.gitignore` ya ignora `*.pbix` y `powerbi/datos/`. Para mostrar el dashboard, publica **capturas de pantalla**.
> Tampoco uses "Publicar en la web" de Power BI Service, porque ese enlace queda abierto a cualquiera.

## 0. Generar los datos

Desde la carpeta del proyecto, con el entorno activado:

```powershell
python entrenar_modelo.py        # Fase 4: modelos y predicciones (si no lo has corrido)
python interpretar_modelo.py     # Fase 5: efectos con intervalos
python preparar_dashboard.py     # crea powerbi/datos/*.csv con nombres anónimos
```

Las cuentas aparecen como "Barbería 01", "Barbería 02"… (01 es la que tiene más posts), igual que en los notebooks.
`python preparar_dashboard.py --con-nombres` deja los usernames reales: úsalo solo para mirar tú, nunca para capturas.

## 1. Configurar Power BI Desktop

1. Abre Power BI Desktop y crea un informe en blanco.
2. **Archivo → Opciones y configuración → Opciones → Archivo actual → Configuración regional → Configuración regional
   para la importación: Inglés (Estados Unidos).** Los CSV usan punto decimal; con la configuración de Chile,
   `0.687` se leería como 687.
3. **Vista → Temas → Examinar temas** y elige `powerbi/tema.json`.

## 2. Importar las tablas

**Inicio → Obtener datos → Texto/CSV**, una vez por archivo de `powerbi/datos/`: `posts`, `cuentas`, `embudo`, `efectos`,
`efectos_simples`, `modelos` y `predicciones`. En la ventana de vista previa, revisa que diga *Origen de archivo:
65001: Unicode (UTF-8)* y pulsa **Cargar**.

Luego **Inicio → Transformar datos** y revisa los tipos:

| Tabla | Columna | Tipo |
|---|---|---|
| posts | `fecha` | Fecha |
| posts | `fecha_hora_local` | Fecha y hora |
| predicciones | `fecha` | Fecha |
| cuentas | `primer_post`, `ultimo_post` | Fecha |
| efectos | `accionable` | Verdadero/falso |

El resto deben ser números enteros o decimales, salvo los textos (`cuenta`, `formato`, `franja`, `dia_semana`, `nombre`…).
Pulsa **Cerrar y aplicar**.

## 3. Relaciones y orden

En la **vista de modelo**, crea estas relaciones (de uno a varios, filtro en una dirección):

- `cuentas[cuenta]` → `posts[cuenta]`
- `cuentas[cuenta]` → `predicciones[cuenta]`

Las tablas `embudo`, `efectos`, `efectos_simples` y `modelos` quedan sueltas.

Para que los ejes salgan en orden lógico y no alfabético, selecciona la columna y usa
**Herramientas de columnas → Ordenar por columna**:

| Columna | Ordenar por |
|---|---|
| `posts[dia_semana]` | `posts[orden_dia]` |
| `posts[franja]` | `posts[orden_franja]` |
| `efectos[nombre]` | `efectos[orden]` |
| `embudo[paso]` | `embudo[orden]` |

## 4. Medidas

Crea una tabla para las medidas: **Inicio → Escribir datos**, nómbrala `Medidas` y pulsa **Cargar**. Selecciónala y
crea cada medida con **Nueva medida**:

```dax
Posts = COUNTROWS ( posts )

Cuentas = DISTINCTCOUNT ( posts[cuenta] )

Likes totales = SUM ( posts[likes] )

Likes típicos por post =
    -- media geométrica: no se deja arrastrar por los pocos posts virales
    EXP ( AVERAGE ( posts[log_likes] ) ) - 1

Likes medianos por post = MEDIAN ( posts[likes] )

% reels = DIVIDE ( CALCULATE ( [Posts], posts[formato] = "Reel" ), [Posts] )

Posts virales = SUM ( posts[viral] )

% de likes del 1% de posts top =
    VAR k = MAX ( 1, ROUND ( [Posts] * 0.01, 0 ) )
    VAR top = TOPN ( k, posts, posts[likes], DESC )
    RETURN DIVIDE ( SUMX ( top, posts[likes] ), [Likes totales] )

Error típico del modelo (x) = EXP ( AVERAGE ( predicciones[error_log_modelo] ) )

Error típico de la referencia (x) = EXP ( AVERAGE ( predicciones[error_log_referencia] ) )

Mejora sobre la referencia =
    1 - DIVIDE ( AVERAGE ( predicciones[error_log_modelo] ), AVERAGE ( predicciones[error_log_referencia] ) )

Color efecto =
    SWITCH (
        SELECTEDVALUE ( efectos[conclusion] ),
        "aumenta los likes", "#2A78D6",
        "reduce los likes", "#E34948",
        "#C9C8C3"
    )

Color comparación =
    IF ( SELECTEDVALUE ( efectos_simples[senal] ) = "clara", "#2A78D6", "#C9C8C3" )
```

Formato: `% reels`, `% de likes del 1% de posts top` y `Mejora sobre la referencia` como **Porcentaje** con 0 decimales.
`Likes típicos por post` sin decimales. Las de error, con 2 decimales.

**Cómo leer el error típico:** 2,0 significa que la predicción suele quedar al doble o a la mitad de los likes reales.

## 5. Páginas

Tamaño recomendado: 16:9. Arriba de cada página, un cuadro de texto con el título y una frase que diga la conclusión.

### Página 1 · Resumen del nicho

*Conclusión sugerida: "Los likes están muy concentrados: unos pocos posts virales se llevan la mayoría."*

| Visual | Campos |
|---|---|
| 4 tarjetas | `Cuentas`, `Posts`, `Likes típicos por post`, `% de likes del 1% de posts top` |
| Gráfico de barras horizontales: embudo de datos | Eje Y `embudo[paso]`, eje X `Sum of cantidad`. Leyenda o filtro por `embudo[etapa]` (o dos gráficos, uno para cuentas y otro para posts) |
| Gráfico de líneas: likes típicos por mes | Eje X `posts[mes]`, eje Y `Likes típicos por post`. Agrega `Posts` en la información sobre herramientas |
| Gráfico de columnas 100% apiladas: mezcla de formatos por mes | Eje X `posts[mes]`, eje Y `Posts`, leyenda `posts[formato]` |
| Gráfico de barras: likes típicos por barbería | Eje Y `posts[cuenta]`, eje X `Likes típicos por post`, orden descendente |

### Página 2 · Qué funciona

*Conclusión sugerida: "Reels y carruseles suman likes; los posts promocionales restan (pero su objetivo es vender)."*

| Visual | Campos |
|---|---|
| Gráfico de barras horizontales: efecto de cada variable | Eje Y `efectos[nombre]`, eje X `Sum of efecto_pct` |
| Barras de error (panel **Análisis** → *Barras de error*) | Límite superior `Sum of efecto_alto_pct`, límite inferior `Sum of efecto_bajo_pct`, relación con la medida: *Absoluta* |
| Color de las barras | **Formato → Barras → Colores → fx**: *Valor del campo* → medida `Color efecto` |
| Tabla: recomendaciones | `efectos[nombre]`, `efecto_pct`, `efecto_bajo_pct`, `efecto_alto_pct`, `recomendacion`. Filtros del visual: `accionable` = Verdadero y `conclusion` distinta de "sin evidencia suficiente" |
| Gráfico de barras: comparaciones dentro de cada cuenta | Eje Y `efectos_simples[comparacion]`, eje X `Sum of mediana_pct`, barras de error con `ic_bajo_pct` / `ic_alto_pct`, color con la medida `Color comparación` |

Agrega una nota de texto: *"Barra azul o roja = el intervalo de confianza de 90% no cruza el 0. Gris = no hay
evidencia suficiente. Son asociaciones observadas, no experimentos."*

### Página 3 · Modelo

*Conclusión sugerida: "Para una barbería con historial, el modelo queda típicamente a ×2 de los likes reales y
mejora un poco a solo mirar su rendimiento reciente."*

| Visual | Campos |
|---|---|
| 3 tarjetas | `Error típico del modelo (x)`, `Error típico de la referencia (x)`, `Mejora sobre la referencia` |
| Gráfico de barras: error de cada modelo | Eje Y `modelos[modelo]`, eje X `Sum of error_tipico_x`. Filtro `modelos[escenario]` = A. Ordena ascendente. Destaca `es_modelo_final` = 1 con formato condicional |
| Mismo gráfico para el escenario B | Filtro `modelos[escenario]` = B. Mensaje: sin historial de la cuenta, ningún modelo le gana a la mediana |
| Gráfico de dispersión: real vs predicho | Eje X `predicciones[likes_predichos_modelo]`, eje Y `predicciones[likes]`, detalles `predicciones[id_post]`. En **Formato** pon los dos ejes en escala **Logarítmica**. Leyenda `predicciones[viral]` |
| Tabla: error por barbería | `cuentas[cuenta]`, `posts_validacion`, `error_tipico_modelo_x`, `error_tipico_referencia_x` con formato condicional (escala de colores) |

### Página 4 · Barberías

| Visual | Campos |
|---|---|
| Segmentación | `cuentas[cuenta]`, selección única |
| Tarjetas | `cuentas[seguidores]`, `Posts`, `Likes típicos por post`, `% reels` |
| Gráfico de líneas: likes en el tiempo | Eje X `posts[fecha]` (jerarquía desactivada), eje Y `Sum of likes`. Agrega una **línea de mediana** desde el panel Análisis |
| Gráfico de barras: likes típicos por formato | Eje Y `posts[formato]`, eje X `Likes típicos por post` |
| Gráfico de columnas: likes típicos por franja | Eje X `posts[franja]`, eje Y `Likes típicos por post` |
| Tabla: posts con más likes | `posts[fecha]`, `formato`, `franja`, `likes`, `comentarios`. Filtro del visual: **N superior** 10 por `Sum of likes` |

## 6. Guardar y capturar

1. Guarda el informe como `powerbi/dashboard_barberias.pbix` (Git lo ignora).
2. Para cada página: **Win + Shift + S** → recorta el lienzo → guarda en `docs/img/` como
   `dashboard_1_resumen.png`, `dashboard_2_que_funciona.png`, `dashboard_3_modelo.png` y `dashboard_4_barberias.png`.
   Antes de capturar, revisa que no aparezca ningún username ni texto de posts.
3. Agrega las capturas al README, en la sección *Dashboard*:

```markdown
![Resumen del nicho](docs/img/dashboard_1_resumen.png)
![Qué funciona](docs/img/dashboard_2_que_funciona.png)
![Modelo](docs/img/dashboard_3_modelo.png)
![Barberías](docs/img/dashboard_4_barberias.png)
```

Si cambias los datos, vuelve a correr `preparar_dashboard.py` y en Power BI pulsa **Inicio → Actualizar**.
