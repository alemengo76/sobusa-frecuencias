# Propuesta de migración 0010 (no aplicada)

Evidencia de completitud por ruta y día. La justificación, el criterio y las pruebas están en `docs/PROPUESTAS_ESQUEMA_ALERTAS.md`, sección P1.

Para aprobarla:

1. Copie `0010_completitud_ruta_dia.py` a `migrations/versions/`.
2. Copie `0010_completitud_ruta_dia.sql` y `0010_completitud_ruta_dia_downgrade.sql` a `migrations/sql/`.
3. Si para entonces existe la 0009 (sentido IDA/VUELTA), cambie `down_revision` a `"0009"`.
4. Aplíquela primero en una base desechable y repita `tests/integracion/test_alertas_conectadas_postgres.py`. Las pruebas de completitud que crean la tabla se omiten solas si la tabla ya existe; en su lugar, compruebe el recorrido con `test_recorrido_automatico_e2e.py` en una instancia nueva.
