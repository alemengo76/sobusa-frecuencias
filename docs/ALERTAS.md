# Alertas y ciclo de datos

El [catálogo](../app/alertas/catalogo.py) contiene ALT-001 a ALT-010. Los umbrales del despliegue provienen de la configuración y el almacenamiento autorizados.

| Alertas | Lógica conservada |
| --- | --- |
| ALT-001 a ALT-004 | Demanda prevista, sobreocupación y frecuencia insuficiente o excesiva. ALT-002 a ALT-004 usan recomendaciones a 60 minutos. |
| ALT-005 y ALT-007 | Comparación entre el pronóstico y los conteos observados disponibles. |
| ALT-006 | Salidas ausentes o tardías según programación, marcas y madurez. |
| ALT-008 | Contraste del consolidado con los conteos por viaje cuando están disponibles. |
| ALT-009 | Detección de información incompleta en días consolidados. La causa «información retrasada» permanece pendiente. |
| ALT-010 | Comparación del WAPE reciente con una referencia cuando existen datos y cobertura suficientes. |

`python -m app.ciclo --desde AAAA-MM-DD --hasta AAAA-MM-DD` ejecuta la ingesta incremental y diagnostica ALT-009. Use únicamente una conexión autorizada. El fin del periodo es exclusivo. Las comprobaciones públicas ejercitan la lógica y los contratos con datos sintéticos. Las lecturas, escrituras y alertas persistidas sobre una base concreta requieren validación en esa instalación.
