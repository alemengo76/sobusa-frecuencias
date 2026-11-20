# RNF-003 · Procesamiento en memoria de 100.000 registros (commit b207557)

Fecha: 2026-11-14. Ejecutado por Mariangel en su Mac.

> **Alcance:** procesamiento EN MEMORIA de registros sintéticos con dos funciones de la ingesta.
> **No incluye** la lectura desde PostgreSQL ni la escritura en `ai_frecuencias`.
> No es una medición de la ingesta completa de extremo a extremo.

## Requisito

RNF-003 (especificación v1.5): "Procesar al menos 100.000 registros con medición de tiempo y memoria."

## Prueba

`tests/unit/test_ingesta.py::test_ingesta_100000_registros_tiempo_y_memoria`, sin modificaciones:

1. Genera un DataFrame sintético de 100.000 viajes con `viajes(100_000)`. La generación queda fuera de la medición.
2. Activa `tracemalloc` y mide el tiempo (`time.perf_counter`) de:
   - `evaluar_programaciones()`: reglas de calidad comun-1.5 y cuarentena;
   - `agregar_por_franja()`: agregación por franjas de 15 minutos.
3. Única aserción: que tarde menos de 30 s. **No hay umbral de memoria**; la memoria solo se mide e imprime.

## Escenario (datos sintéticos, verificados en el código de `b207557`)

| Característica | Valor |
|---|---|
| Registros | 100.000 viajes sintéticos (semilla 0) |
| Ruta | una sola, `idruta` 16 |
| Salidas | 2025-03-04, entre 06:00 y 06:59, es decir, 4 franjas de 15 minutos |
| Pasajeros | enteros entre 20 y 119 |
| Estado | todos `finalizado = "S"` y `cancelada = "N"` |
| `fechainicio`, `fechafin` | vacías (`NaT`) |
| `numerobajadas` | no incluida |

Lo que esto implica para las reglas de calidad (`app/fe31/reglas_comunes.py`):

- `pasajeros_max` es 500, así que ningún viaje activa `pasajeros_extremo`.
- Con `fechainicio` y `fechafin` vacías, la duración queda vacía. No se activan las reglas de duración negativa, mayor a 24 h, inicio incoherente ni fin tardío, pero tampoco se puede evaluar el ciclo (`apto_ciclo` es falso). **No son viajes con tiempos completos.**
- Sin `numerobajadas`, la regla de bajadas no aplica.
- Por las características de los datos sintéticos, las reglas de cuarentena revisadas no deberían activarse; sin embargo, la prueba no registra un conteo explícito de registros enviados a cuarentena.

## Entorno

| Dato | Valor |
|---|---|
| Código | commit `b207557` (`main`) |
| Imagen | `sobusa-prototipo-integracion-app`, ID `41d926d3af32`, `linux/arm64` |
| Python / pytest | 3.11.18 / 9.2.0 |
| Equipo | MacBook Air M2, Docker Desktop 29.9.3 (8 CPU y unos 7,75 GiB asignados a Docker) |
| Aislamiento | contenedor temporal sin red (`--network none`), sin `.env` y sin base de datos |

## Reproducción

```
docker run --rm --network none --entrypoint python sobusa-prototipo-integracion-app \
  -m pytest -p no:cacheprovider -s tests/unit/test_ingesta.py -k 100000
```

`-p no:cacheprovider` evita una advertencia de pytest por no poder escribir su caché en `/app`, ya que la imagen corre con un usuario sin privilegios. No afecta la medición.

## Resultado

| Medida | Ejecución 1 | Ejecución 2 |
|---|---|---|
| Registros procesados | 100.000 | 100.000 |
| Tiempo | 0,17 s | 0,17 s |
| Memoria pico (`tracemalloc`) | 27,3 MB | 27,3 MB |
| Resultado de la prueba | `1 passed` | `1 passed` |

Los valores coinciden tal como los imprime la prueba (tiempo con dos decimales, memoria con uno). Las salidas de las dos ejecuciones no se guardaron en archivo; los valores salen de la terminal.

## Limitaciones

- Solo procesamiento en memoria: no mide la lectura desde PostgreSQL, la escritura en `ai_frecuencias`, la marca de agua ni la deduplicación en la base.
- `tracemalloc` mide las asignaciones de memoria rastreadas por Python, no el consumo total del proceso.
- El tiempo se mide con `tracemalloc` activo, que agrega costo a cada asignación.
- Datos sintéticos uniformes (una ruta, una hora y sin fechas de inicio y fin); el resultado no representa la variedad de los datos reales.
- Dos ejecuciones no bastan para estimar la variabilidad de una medición tan corta.

## Evidencia complementaria pendiente

Se reportó una ingesta real de 564.837 viajes en la base de Alejandra el 7 de noviembre. Está pendiente de confirmar su duración, qué operaciones incluyó y si existe un registro verificable. Hasta confirmarlo, no se presenta como evidencia de rendimiento de extremo a extremo.
