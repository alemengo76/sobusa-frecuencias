# Decisiones de la edición de demostración

- La API conserva las operaciones originales. La selección empresarial se basa en el modelo activo del registro y conserva el respaldo.
- En una base nueva, las migraciones cargan `baseline_dh_v1` y un perfil operativo enteramente sintético. El perfil verifica capacidad, factores, ocupación, viabilidad y alertas. El perfil de una base empresarial existente se administra a través de su configuración autorizada.
- Los artefactos entrenados, vehículos, datos reales y resultados retrospectivos se suministran localmente mediante un procedimiento privado. Las pruebas públicas ejercitan el contrato de pronóstico, intervalo del GBM y frecuencias con insumos sintéticos.
- La preparación local no ha ejecutado migraciones ni operaciones contra PostgreSQL productivo.
