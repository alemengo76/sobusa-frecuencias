# I2 · Verificación de dependencias congeladas (entorno de Mariangel)

Fecha: 2026-11-13. Solo lectura: sin `db`, sin migraciones, sin `.env`, contenedores temporales con
`docker run --rm --network none`.

## Entorno

| Dato | Valor |
|---|---|
| Equipo | MacBook Air M2; Docker Desktop 29.9.3 (`aarch64`), 8 CPU, 8.319.705.088 B |
| Virtualización | Apple Virtualization framework, Rosetta activado, VirtioFS |
| Repositorio | `b207557` (pull --ff-only el 2026-11-09 20:27 -0500), sin cambios locales |
| Proyecto Compose | `sobusa-prototipo-integracion` (definido en `.env`) |
| Imagen app | `41d926d3af32`, `linux/arm64`, construida 2026-11-10T01:40:25Z (13 min después del pull), modo internet |
| Código de la imagen | Idéntico a `b207557`: 83 archivos de `app`, `migrations`, `scripts`, `tests` con el mismo SHA-256 |
| Ejecución real | app `aarch64` (nativa); `postgis/postgis:14-3.4` (`d803ac7dab61`) `x86_64` (emulada con Rosetta) |

## Resultado

| Verificación | Resultado |
|---|---|
| Lista | `docker/requisitos-congelados.txt`, commit `3571ae7`, 87 paquetes |
| `pip freeze` de la imagen vs. lista (nombres normalizados) | Sin diferencias, 87 = 87 |
| `scripts/verificar_wheelhouse.py --instalados` | 87 paquetes fijados comprobados; 0 problemas |
| Python / pip | 3.11.18 / 24.0 |
| `pip check` | No broken requirements found |

Archivos: `i2_freeze_imagen.txt` (pip freeze de la imagen) e `i2_lista_normalizada.txt` (la lista, con
el mismo formato).

## Conclusión

La imagen ARM64 construida por internet contiene exactamente los
87 paquetes y versiones definidos en `docker/requisitos-congelados.txt`.
La verificación de `scripts/verificar_wheelhouse.py --instalados`
reportó 0 problemas y `pip check` no detectó dependencias rotas.

El resultado corresponde a ESTA imagen y no demuestra que una
reconstrucción futura en modo internet conserve las mismas versiones.
Tampoco constituye, por sí solo, una comparación entre dos instalaciones
ejecutadas en arquitecturas diferentes.

Propuesta para un PR independiente: instalar con
`-c docker/requisitos-congelados.txt` también en modo internet,
sin modificar las versiones congeladas.
