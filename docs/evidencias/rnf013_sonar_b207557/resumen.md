# RNF-013 · Análisis estático con SonarQube (PRELIMINAR, commit b207557)

Fecha: 2026-11-14. Ejecutado por Mariangel en su Mac (M2).

Este análisis es preliminar. Debe repetirse sobre el `main` final, con P1 integrado, para constituir la evidencia de entrega.

## Entorno

| Dato | Valor |
|---|---|
| Servidor | SonarQube Community Build 26.11.0.134175, modo MQR, imagen `sonarqube@sha256:7a8f5e1c39743e1a064dd68bdcd347706988e41d7875cb6e1139ec0e4282111c` (linux/arm64). Base de datos embebida, solo para evaluación |
| Aislamiento | Solo en `127.0.0.1:9100`. Contenedor, red y volúmenes propios (`sobusa_sonar_*_20261114`); no monta ni usa recursos de SOBUSA |
| Requisitos | `vm.max_map_count` modificado temporalmente de 262144 a 524288 en la máquina virtual de Docker Desktop. Restauración pendiente de verificar. `--ulimit nofile=131072 nproc=8192`; `--memory 5g` |
| Escáner | SonarScanner CLI 8.3.1.16491 macOS aarch64. SHA-256 `feea9890c330c6ceb32333cb22629bec2241607cdf1cd1129208bb5ecd77f5da`, verificado contra el `.sha256` oficial |
| Código | `main` en `b2075570df7ed7a115185cc56ccf069b2373d6be`, árbol limpio. Versión declarada: `baseline-b207557` |
| Perfiles | Predeterminados `Sonar way core`: Python 232 reglas activas y Docker 18. Existen perfiles más amplios de Python (extended 266 y comprehensive 407) que no se aplicaron |
| Código nuevo | Previous version |

## Alcance

El registro informa 85 archivos preprocesados y dos lenguajes detectados:

- 54 archivos `.py` de fuentes.
- 20 archivos `.py` de pruebas.
- 1 `Dockerfile`.
- 9 archivos `.sql` de `migrations/sql/`.
- 1 plantilla `migrations/script.py.mako`.

Total: 85 archivos preprocesados.

Las métricas de código contabilizan 55 archivos y 3.658 líneas: 3.628 de Python y 30 de Docker.

## Resultado

| Medida | Valor |
|---|---|
| Vulnerabilidades | 0 |
| Bugs | 0 |
| Puntos sensibles de seguridad | 0 |
| Hallazgos abiertos | 2 |
| Duplicación | 0,0 % |
| Cobertura | 0,0 % (no se envió reporte de cobertura) |
| Puerta de calidad | OK |

Los dos hallazgos abiertos corresponden a `python:S6709`, por falta de semilla en `random_state`:

- `app/ml/gbm.py:49`
- `app/ml/modelo_gbm.py:105`

Ambos son `CODE_SMELL`, severidad `MAJOR` en la clasificación tradicional. Sus impactos MQR son mantenibilidad Medium, confiabilidad Medium y seguridad Low.

No hay hallazgos de severidad crítica o bloqueante pendientes.

En esta versión, las reglas de seguridad comprobadas para Python están clasificadas como `VULNERABILITY`: S5332, S2245, S4507 y S5443. `python:S4721` no existe en esta versión.

La regla `docker:S6471` está activa en el perfil predeterminado `Sonar way core` de Docker. La página de la regla muestra cero incidencias en las ramas principales del proyecto. Esto se refiere exclusivamente a esa regla.

## Conclusión preliminar

En el análisis de `b207557` no se detectaron vulnerabilidades, puntos sensibles de seguridad ni hallazgos críticos o bloqueantes pendientes. Los dos hallazgos `MAJOR` de mantenibilidad y confiabilidad quedan documentados.

**RNF-013 no se declara cumplido todavía.** Es necesario repetir el análisis sobre el `main` final, con P1 integrado.

## Limitaciones

- Se aplicó el perfil predeterminado `Sonar way core`, no el perfil más amplio.
- Cobertura 0 %: no se proporcionó un reporte de cobertura.
- Los archivos SQL y la plantilla Mako fueron preprocesados, pero no cuentan con análisis de lenguaje en esta instalación.
- `docker-compose.yml` no fue analizado mediante reglas YAML.
- Hubo un primer análisis a las 15:33 UTC sin el `Dockerfile`; el análisis válido es el segundo, a las 15:53 UTC.
- La ruta personal del registro del escáner fue reemplazada por `~`.

## Para Alejandra

`python:S6709` señala que el GBM no fija `random_state` (`app/ml/gbm.py:49` y `app/ml/modelo_gbm.py:105`). Puede afectar la reproducibilidad del modelo y el requisito de registrar la semilla. No se modificó desde esta rama porque cambiarlo podría alterar el artefacto y su hash.

Se debe decidir si se fija la semilla y se reentrena, o si se documenta por qué el resultado actual ya es reproducible.
