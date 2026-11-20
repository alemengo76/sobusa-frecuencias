# Evidencia: regresión unitaria y de API

**Fecha:** 17 de noviembre de 2026  
**Entorno:** Contenedor `sobusa_api_prueba`  
**Python:** 3.11.18  
**Comando ejecutado:**

```bash
docker exec -w /app sobusa_api_prueba python -m pytest -q -rs tests/unit tests/api
```

## Resultado

- **Pruebas aprobadas:** 328
- **Pruebas fallidas:** 0
- **Pruebas omitidas:** 2
- **Advertencias:** 925
- **Duración:** 15.18 segundos

## Pruebas omitidas

Las dos pruebas omitidas corresponden a `tests/unit/test_fe31.py:23`:

1. No está disponible `/fe_v4_comun15/conjunto_entrenamiento.py`.
2. No está disponible `/fe_v4_comun15/reglas_comunes.py`.

## Advertencias

Se registraron principalmente advertencias relacionadas con:

- Uso de `dayofweek` en Pandas, marcado como obsoleto.
- Uso de `httpx` con `starlette.testclient`, cuya compatibilidad está deprecada.

No se modificaron dependencias ni se realizaron cambios en el código para esta verificación.

## Conclusión

La suite `tests/unit` y `tests/api` finalizó satisfactoriamente: 328 pruebas aprobadas y ninguna fallida. Las dos pruebas omitidas dependen de archivos externos no disponibles en el entorno.

**Alcance:** esta evidencia no representa la ejecución de pruebas de integración ni la validación de rendimiento R1 y R2.