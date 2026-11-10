# Construir la imagen sin red (wheelhouse)

**Para qué sirve.** Con una red lenta o que corrompe descargas, `pip install` dentro de `docker compose build` falla después de horas: por *timeout*, o por un SHA-256 distinto al publicado por PyPI. El wheelhouse resuelve esto en dos pasos:

1. Primero se descargan los paquetes, uno por uno. Las descargas se pueden reanudar y cada archivo se verifica.
2. Después la imagen se construye **sin red** (`RUN --network=none`), usando solo esa carpeta.

**Lo que no cambia.** Sin variables, `docker compose build` sigue funcionando como antes (modo `internet`). No se desactiva ninguna verificación de hashes y no cambia ninguna versión de dependencias.

## Archivos

| Archivo | Qué es | ¿En git? |
|---|---|---|
| `docker/requisitos-congelados.txt` | Versión exacta de cada dependencia, tomada de una imagen que ya funcionó (paso 0) | Sí |
| `docker/requisitos-construccion.txt` | Herramientas de construcción fijadas con `==` (hoy `setuptools==80.9.0`) | Sí |
| `scripts/preparar_wheelhouse.py` | Fase A (`descargar`), fase B (`construir`) y `estado` | Sí |
| `scripts/verificar_wheelhouse.py` | Valida la carpeta y escribe `SHA256SUMS`; con `--instalados`, revisa lo que quedó instalado en la imagen | Sí |
| `scripts/wheelhouse_comun.py` | Código compartido por los dos scripts | Sí |
| `docker/instalar_wheelhouse.sh` | Lo que ejecuta el Dockerfile en modo `wheelhouse` | Sí |
| `docker/wheelhouse-vacio/` | Carpeta vacía que se usa en modo `internet` | Sí |
| `wheelhouse/` | Las ruedas, los fuentes y `ORIGEN.jsonl` | **No** (`.gitignore` y `.dockerignore`) |

## Paso 0: generar la lista congelada (una sola vez)

Se hace en un equipo cuya imagen ya se construyó y pasó las pruebas (hoy, el Mac de Mariangel). Desde la raíz del repositorio:

```bash
docker compose run --rm -T --no-deps app python -m pip freeze --exclude-editable \
  | grep -v '^sobusa-frecuencias' > docker/requisitos-congelados.txt
```

- Hay que usar `bash`/`zsh`. En PowerShell, `>` guarda el archivo en UTF-16, y el script lo rechaza.
- Revise que el archivo tenga solo líneas `paquete==versión`. Debe incluir `scikit-learn==1.9.1`.
- `pip freeze` no incluye `pip` ni `setuptools`: `setuptools` se fija en `requisitos-construccion.txt`.
- El archivo se versiona en git, para que todos construyan con las mismas versiones.

## Comandos (en Windows, PowerShell, desde la raíz del repositorio)

Todos los comandos corren dentro de `python:3.11-slim` para `linux/amd64`. Es la misma imagen base del Dockerfile, así que las ruedas que se eligen son las que esa imagen puede instalar. Si el intérprete no es Python 3.11 en Linux x86_64, el script se niega a correr. En el Mac, cambie `${PWD}` por `"$PWD"`.

### Fase A: descargar (con red)

```powershell
docker run --rm --platform linux/amd64 -e PYTHONDONTWRITEBYTECODE=1 -v "${PWD}:/src" -w /src python:3.11-slim python scripts/preparar_wheelhouse.py descargar
```

Por cada versión de las dos listas:

- Consulta qué archivos publica PyPI. Si hay una rueda compatible, baja solo esa.
- Si no hay rueda compatible pero sí código fuente, baja el fuente a `wheelhouse/fuentes/` para la fase B.
- Un error de red, de consulta o de hash **nunca** hace que pase a usar el código fuente: deja la versión PENDIENTE.

El resultado se lee así:

- **Código de salida 2** (`… pendientes`): **repita el mismo comando**. Lo que ya está completo no se vuelve a bajar. Una descarga cortada continúa desde el byte en que quedó (`.tmp/<archivo>.part`).
- **Código 0**: se termina la fase A. Si dice `por construir`, siga con la fase B; si no, pase a validar.
- `PENDIENTE … SHA-256 distinto al publicado`: el archivo dañado se aparta a `.corruptos/` (no se borra) y se descarga de nuevo al repetir.
- `PENDIENTE … no está fijado en docker/requisitos-construccion.txt`: un fuente necesita una herramienta que no está fijada. Agréguela con `==` y repita la fase A.

### Fase B: construir los fuentes (sin red)

Solo hace falta si la fase A dijo `por construir`.

```powershell
docker run --rm --platform linux/amd64 --network none -e PYTHONDONTWRITEBYTECODE=1 -v "${PWD}:/src" -w /src python:3.11-slim python scripts/preparar_wheelhouse.py construir
```

- Si detecta red, se niega a correr.
- Crea un entorno virtual nuevo. Instala ahí **solo** `requisitos-construccion.txt`, desde la carpeta (`--no-index`), y comprueba versiones exactas y `pip check`.
- Por cada fuente revisa su `[build-system] requires`: todo debe estar fijado con `==`.
- Construye con `pip wheel --no-deps --no-build-isolation --no-index --check-build-dependencies`. Si una construcción intenta descargar algo, falla y dice qué intentó (detalle en `wheelhouse/.registros/`).
- Se puede repetir: las ruedas ya construidas se conservan.

### Validar (sin red)

```powershell
docker run --rm --platform linux/amd64 --network none -e PYTHONDONTWRITEBYTECODE=1 -v "${PWD}:/src" -w /src python:3.11-slim python scripts/verificar_wheelhouse.py
```

Comprueba, sin modificar nada:

- que haya exactamente una rueda por versión de las listas, y nada de más;
- que cada rueda sea instalable en ese intérprete;
- los hashes:
  - **rueda de PyPI**: el archivo, el registro y lo publicado coinciden;
  - **rueda construida**: el archivo coincide con el registro, y su fuente coincide con su registro y con lo publicado;
- que las ruedas construidas se hayan hecho con las herramientas fijadas hoy.

Si todo está bien, escribe `wheelhouse/SHA256SUMS`. Si algo falla, no lo escribe, y un `SHA256SUMS` anterior pasa a llamarse `SHA256SUMS.invalido`.

Con `--pypi` (y con red), además vuelve a consultar los hashes publicados en PyPI.

### Construir la imagen (sin red)

```powershell
$env:PIP_MODO="wheelhouse"; $env:WHEELHOUSE_DIR="./wheelhouse"; docker compose build app
Remove-Item Env:PIP_MODO, Env:WHEELHOUSE_DIR
```

El Dockerfile ejecuta `docker/instalar_wheelhouse.sh` con `--network=none`:

1. `sha256sum -c SHA256SUMS`, y comprueba que las ruedas de la carpeta sean exactamente las listadas.
2. Instala las herramientas de `requisitos-construccion.txt` desde la carpeta.
3. Comprueba que quedaron instaladas en la versión fijada.
4. Instala el proyecto con `--no-index --find-links --only-binary=:all: --no-build-isolation --check-build-dependencies`, con las dos listas como restricciones.
5. Ejecuta `pip check` y comprueba que **cada** paquete de las dos listas quedó en su versión exacta.

### Ver el estado en cualquier momento (no modifica nada)

```powershell
docker run --rm --platform linux/amd64 -v "${PWD}:/src" -w /src python:3.11-slim python scripts/preparar_wheelhouse.py estado
```

## Garantías y recuperación

**Registro en `ORIGEN.jsonl`.** Cada archivo tiene una línea JSON; si hay varias, vale la última. Guarda el tipo, el origen (`pypi` o `construida`), el `sha256` y el `sha256_indice`. En las ruedas construidas guarda además el fuente, su `sha256_fuente` y las herramientas usadas.

**Orden de escritura.** Archivo verificado en `.tmp/` → línea en el registro (con `fsync`) → `os.replace` a su lugar. Un archivo cuenta como completo solo si está en su lugar, tiene registro y su hash coincide.

**Al empezar, cada fase revisa la carpeta:**

| Situación | Qué hace |
|---|---|
| Registrado, pero sigue en `.tmp/` (se cortó justo antes de moverlo) | Termina el movimiento |
| Registrado, pero no existe | Lo vuelve a hacer |
| Existe sin registro | Lo aparta a `.huerfanos/` y lo vuelve a hacer |
| Hash distinto al registrado | Lo aparta a `.corruptos/` y lo vuelve a hacer |
| Versión que ya no está en las listas | La aparta a `.retirados/` |
| Línea incompleta en el registro (corte a mitad de escritura) | La ignora con un aviso |

**Nada se borra.** Las carpetas `.huerfanos/`, `.corruptos/` y `.retirados/` se pueden revisar y vaciar a mano.

**Una ejecución a la vez.** Se usa `wheelhouse/.tmp/BLOQUEO`. Ctrl+C lo libera. Si el equipo se apagó en medio, el script dice que hay otra ejecución: si no la hay, mueva ese archivo fuera de la carpeta y repita.

## Límites conocidos

- La fase A depende de la red. Avanza archivo por archivo y reanuda, pero no la hace más rápida.
- Si la API JSON de PyPI no responde desde su red, todo queda PENDIENTE y el script lo dice. Nunca pasa a usar el código fuente por ese motivo.
- Un paquete que solo se pueda compilar con herramientas del sistema (un compilador de C, por ejemplo) falla en la fase B con su nombre. Hoy no se espera ninguno: las dependencias publican ruedas para Linux x86_64.
- Necesita Docker Compose 2.17 o más reciente (`additional_contexts`) y BuildKit (`RUN --network=none`, `--mount`).
