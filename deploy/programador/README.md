# Programador del ciclo diario: configuración para activar

Nada de esta carpeta está instalado ni desplegado. Para activarlo hacen falta las autorizaciones de la sección «Antes de activar».

## Qué hace

`python -m app.programador` es un proceso que queda corriendo y dispara cada trabajo a su hora, en hora de Bogotá:

| Variable | Trabajo | Qué registra |
| --- | --- | --- |
| `PROGRAMADOR_CICLO_HORAS` | `app.ciclo_alertas.correr()` | El intento de ingesta, que se registra antes de empezar. Después, la ejecución `alertas`: completitud, ALT-009 por retraso, ALT-005/007, ALT-010, ALT-006 y ALT-008 |
| `PROGRAMADOR_VERIFICADOR_HORAS` | `app.ciclo_alertas.verificar_retraso()` | Una ejecución `alertas` solo con ALT-009 por retraso. **No ingiere**, así que corre aunque la ingesta no haya arrancado |
| `PROGRAMADOR_FRECUENCIAS_HORAS` (opcional) | `servicios.ejecutar_frecuencias()` para el día siguiente | Predicción, recomendación, ALT-001 a ALT-004 e incompletitud de ALT-009 |

Cómo se comporta:

- **Cada trabajo corre en su propio hilo.** Si el ciclo se cuelga o falla, el verificador se dispara igual a su hora.
- **Un trabajo no se superpone consigo mismo.** Si a la hora siguiente sigue corriendo, ese disparo se omite y el registro lo marca como `omitido_en_curso`.
- **Cada disparo tiene su propio `correlation_id`** (`<PROGRAMADOR_PREFIJO>-<trabajo>-<AAAAMMDDTHHMMSS>`). Se propaga a todas las ejecuciones que ese disparo crea.
- **No hay horarios por defecto.** Si falta `PROGRAMADOR_CICLO_HORAS` o `PROGRAMADOR_VERIFICADOR_HORAS`, el proceso termina con código 2 y no programa nada.
- **Comprobación sin ejecutar:** `python -m app.programador --comprobar` valida la configuración y muestra los próximos disparos.

## Cómo activarlo (cuando esté autorizado)

1. Copie `programador.env.ejemplo` a `programador.env` y complete las horas reales y la política de ALT-009. Ese archivo está en `.gitignore`.
2. Ejecute:

   ```bash
   docker compose -f docker-compose.yml -f deploy/programador/docker-compose.programador.yml \
       --profile programador up -d programador
   ```

   El servicio tiene el perfil `programador`, así que un `docker compose up` normal **no** lo arranca. Su comando es el programador: no aplica migraciones.

**Alternativa sin Docker.** Un servicio de systemd o una línea de cron que deje corriendo `python -m app.programador` con las mismas variables. Si se prefiere cron para cada disparo, se puede llamar `python -m app.ciclo_alertas` y `python -m app.ciclo_alertas --solo-verificar-retraso` a las horas acordadas. En ese caso, cada invocación debe llevar su `correlation_id`, que se pasa en el código.

## Antes de activar (autorizaciones que faltan)

1. **Horas reales del ciclo y del verificador.** Deben salir de la política de ALT-009 por retraso (apertura, vencimiento y tolerancia), que está pendiente de validación con Extreme o con el responsable operativo (DECISIONES §16.2).
2. **Política de ALT-009 por retraso** en las variables `ALT009_RETRASO_*`. Sin ella, el retraso es `no_evaluable`.
3. **Aprobación del despliegue** del servicio en el entorno donde corre la aplicación.

## Cómo quedó demostrado

- **Pruebas unitarias con reloj simulado** (`tests/unit/test_programador.py`):
  - cada trabajo se dispara a su hora y nunca antes;
  - el verificador corre mientras el ciclo sigue colgado;
  - un ciclo que falla al arrancar no detiene el programador;
  - sin configuración, el proceso termina con código 2.
- **Prueba de punta a punta con el proceso real** (`tests/integracion/test_recorrido_automatico_e2e.py`): horas de prueba a pocos segundos, en una base aislada y con fuentes sintéticas. Ver `docs/ALERTAS_ALT001_ALT010.md`.
