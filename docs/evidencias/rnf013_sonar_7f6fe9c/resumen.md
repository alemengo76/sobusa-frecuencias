# RNF-013 · Evidencia de SonarQube para E03

- Revisión: `7f6fe9c25eb9112ac949edf1eb3004beb7634ab9`.
- SonarQube Community Build: `26.11.0.134175`.
- Puerta de calidad: `OK`. La respuesta de la API trae una lista de condiciones vacía.
- Criterio RNF-013 evaluado por el job: 0 vulnerabilidades y 0 hallazgos críticos o bloqueantes.
- Fuentes: `app/`, `migrations/` y `Dockerfile`. Pruebas: `tests/`. No se importó cobertura. Incluir las migraciones en el alcance no acredita análisis SQL.
- Fuera del alcance: el resto del árbol, incluidos `dashboard/`, `deploy/`, `scripts/`, `propuestas/`, `docs/` y los workflows de GitHub Actions. Se aplicaron además las exclusiones de la evidencia anterior para archivos `.env*`, datos, respaldos y copias, modelos, wheelhouse, resultados, cachés de Python, entornos virtuales y documentación.

[Ejecución](https://github.com/alemengo76/sobusa-frecuencias/actions/runs/45936303690)
[Artefacto original](https://github.com/alemengo76/sobusa-frecuencias/actions/runs/45936303690/artifacts/15170856759)

SHA-256 del ZIP original: `2088ca4bb52e57e1edd857ddfc9de3040df244b2aceac545423c3c7da1e37d37`. Se verificó que el SHA coincide en los metadatos de GitHub, `sha.txt`, `analisis.json` y `hallazgos.json`. El ZIP original conserva también los logs operativos del contenedor.
