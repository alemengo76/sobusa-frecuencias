# Propuestas de esquema, fuentes y autorizaciones para las alertas: NO aplicadas

Nada de esto se aplicó en ninguna base de trabajo. La revisión de Alembic del repositorio sigue en **0008**.

La propuesta 0010 está en `propuestas/migraciones/`, **fuera** de `migrations/versions/`: `alembic upgrade head` no la aplica. Solo se aplicó dentro de bases de prueba aisladas:
- en una transacción que se revierte;
- en la instancia exclusiva del recorrido automático.

**Numeración.** DECISIONES reserva la **0009** para la migración de sentido IDA/VUELTA (§14). Por eso la propuesta es la **0010**, con `down_revision = "0008"`. Si la 0009 se aprueba antes, hay que cambiar ese valor a `"0009"`.

## P1. Completitud por ruta y día: desbloquea ALT-005, ALT-007 y ALT-010

**Antecedente.** No existe un procedimiento del equipo para obtener ni cargar esta evidencia: lo busqué en el repositorio, en los documentos del proyecto y en la copia de GitHub del equipo. Por eso entrego las tres piezas:

| Pieza | Dónde | Qué hace |
| --- | --- | --- |
| Tabla (propuesta) | `propuestas/migraciones/0010_completitud_ruta_dia.sql`, `.py` y su reversión | Crea `completitud_ruta_dia` (estado vigente con evidencia, regla y ejecución) y `completitud_ruta_dia_historial` (la versión anterior de cada cambio). Solo objetos nuevos en `ai_frecuencias` |
| Mecanismo que la llena | `app/calidad/completitud.py` (`calcular`, `registrar`) y `flujo.evaluar_completitud` | Corre en cada ciclo diario, antes de ALT-005, ALT-007 y ALT-010. Recalcula los últimos 35 días consolidados. Solo escribe lo que cambió y guarda antes la versión anterior en el historial |
| Fuente de referencia | `fuentes_extreme.ProgramacionesPorDia` | Lee `progvehiculos` de Extreme con el usuario de solo lectura y da las programaciones por ruta y día |

**Criterio `completitud-v1`.** Un día queda `completo` si cumple estas cuatro condiciones:
1. está consolidado;
2. la última ingesta terminó exitosa, completa y con consulta válida;
3. las programaciones de Extreme para ese día son más de 0 y coinciden con los viajes copiados;
4. la cobertura de conteos es mayor o igual al umbral de ALT-009 (0,90).

Si se cumplen 1 a 3 pero no 4, o si la copia no coincide, queda `parcial`. En cualquier otro caso queda `desconocido`.

Este criterio **no certifica** el universo de viajes de Extreme (DECISIONES §16.6).

**Sin la tabla.** El mecanismo calcula, no escribe y responde `tabla_pendiente_de_aprobacion`.

**Riesgo.** Un criterio laxo produciría alertas sobre días incompletos. Por eso la decisión que hay que aprobar es tanto el criterio como la tabla.

**Impacto sobre las alertas abiertas.** No cambia claves ni estados. Después de aprobarla, los ciclos siguientes pueden abrir alertas ALT-005, ALT-007 y ALT-010 para los días que queden `completo`.

**Pruebas:**
- `test_completitud_llena_la_tabla_con_evidencia_y_desbloquea_005_007`, con rollback;
- `test_completitud_sin_tabla_calcula_pero_no_escribe`;
- el recorrido automático de `test_recorrido_automatico_e2e.py`.

## P2. Política de calendario de ALT-009 por retraso en la base (opcional)

Hoy la política vive en las variables `ALT009_RETRASO_*`, sin valores por defecto. Cuando se valide, se puede crear la migración **0011** con `ai_frecuencias.politica_alerta`. Columnas propuestas:
- código, condición y fuente;
- apertura, vencimiento y tolerancia_min;
- versión y vigente;
- aprobado_por y aprobado_en.

**Riesgo.** Cambiar la apertura mueve la asignación de los intentos a sus ciclos. Conviene cambiarla sin episodios abiertos.

## P3. Severidad Crítica de ALT-009

No migrar. La equivalencia técnica `alta` está pendiente de aprobación. Cuando se apruebe, hay que alinear el catálogo: hoy `alert_catalog.severidad_base` de ALT-009 es `media`.

## P4. Fuentes de ALT-006 y ALT-008

**ALT-006 (ausencias).** Hace falta una fuente aprobada que acredite que la fuente de despachos cubre una ruta y un día. En el diccionario hay candidatos, pero ninguno tiene una semántica confirmada:
- `progvehiculos.excedeplazosalida`;
- `activa`;
- `gtransid`;
- eventos de `conteo_pasajeros_destino`;
- el registro de transmisión del equipo a bordo.

Se conecta por el parámetro `evidencia_cobertura` de `motor.alt_006`. Las salidas **tardías** ya están conectadas con `fechasalida` y `fechainicio`.

**ALT-008.** Está conectada a `timbradas_anio` (referencia de Extreme) frente a `progvehiculos` (viajes), las dos de solo lectura. Para activarla, Extreme debe confirmar:
1. qué representa `timbradas_anio.subidas`;
2. que su período es el año de `COALESCE(fecha_fin_liquidacion, fechafin)`;
3. si el hallazgo §9.1 de `fn_insert_timbradas_anio` es real.

Después se habilita `ALT008_REFERENCIA=timbradas_anio`.

Si se prefiere otra referencia, hay que pedir una vista de solo lectura con:
- campos: `idruta`, `periodo_inicio`, `periodo_fin`, `granularidad` (día o mes), `subidas`, `viajes`, `fuente_calculo` y `actualizado_en`;
- una condición: que se calcule de forma independiente de `progvehiculos.numeropasajeros`, por ejemplo desde `conteo_pasajeros_destino`.

## P5. Verificación de la autorización de reintentos manuales

`--autorizado-por` solo registra una declaración. El equipo debe definir:
1. quién autoriza;
2. dónde queda la autorización;
3. cómo la comprueba `verificar_autorizacion(intento)`.

Mientras tanto, ningún reintento manual cuenta.

## P6. Despliegue del programador

La configuración está lista en `deploy/programador/` (perfil `programador`, apagado por defecto). Para activarla se necesitan:
1. las horas reales, que dependen de P2;
2. la aprobación del despliegue en el entorno donde corre la aplicación.
