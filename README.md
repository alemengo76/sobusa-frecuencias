# SOBUSA · Bandeja de alertas en Render

Rama preparada: `deploy/render-alertas-consulta`. Contiene solo la aplicación de consulta y los scripts de preparación de una base vacía.

## Procedencia

- La API local de 18030 era `work/SOBUSA_api_sobusa_main.py` (SHA-256 `9e5c244cb23be7aa7bae687b03c377d33f63f50fcd8776595099e4f58bb7e304`) y cargaba `work/sobusa-main-e3b15b2/app/api/main.py`. Usaba FastAPI 0.162.2, Uvicorn 0.54.0 y psycopg 3.3.8 en las versiones fijadas de la copia. Esa envoltura apuntaba a la base de trabajo y consultaba Extreme al arrancar. La versión de Render reproduce el contrato GET de alertas y su paginación con una proyección separada.
- El tablero local de referencia es `work/sobusa-main-e3b15b2/dashboard/tablero.py` (SHA-256 `3467bc6ac98bb3953cbeb50aa04c399af340bd83a19534922ae4241c5516a66a`). El tablero completo importa `cliente.py`, `estilo.py`, `explicacion.py`, `exportacion.py` y `graficos.py`. La bandeja de consulta reutiliza íntegros `cliente.py` y `estilo.py` y adapta la sección de alertas. El entorno local de Streamlit registra Streamlit 1.67.0, pandas 3.1.1, requests 2.34.2 y openpyxl 3.1.5. Para esta bandeja se fijan los tres primeros; openpyxl correspondía a la exportación Excel.
- Las rutas anteriores son relativas a `C:\Users\Alejandra\Documents\Codex\2026-11-20\quiero-que-contin-es-mi-proyecto`.

## Crear los servicios

Crea primero Postgres con plan **Free** y nombre de base `sobusa_alertas_render`. Usa la misma región en los tres recursos.

| Campo de Render | API de alertas | Tablero |
|---|---|---|
| Tipo | Web Service | Web Service |
| Rama | `deploy/render-alertas-consulta` | `deploy/render-alertas-consulta` |
| Runtime | Python 3 | Python 3 |
| Root Directory | `api` | `dashboard` |
| Build Command | `python -m pip install -r requirements.txt` | `python -m pip install -r requirements.txt` |
| Start Command | `python -m uvicorn main:app --host 0.0.0.0 --port $PORT` | `python -m streamlit run tablero.py --server.address 0.0.0.0 --server.port $PORT --server.headless true --browser.gatherUsageStats false` |
| Plan | Free | Free |
| Health Check Path | `/healthz` | `/_stcore/health` |
| Variables de entorno | `PYTHON_VERSION=3.11.9` y `DATABASE_URL` del lector | `PYTHON_VERSION=3.11.9` y `API_URL=https://<servicio-api>.onrender.com/api/v1` |

Deja vacío el comando de predespliegue. El servicio API monta solo GET para alertas y salud. Los servicios no ejecutan Alembic, el ciclo automático ni notificaciones. Coloca la URL de la base como secreto en Render. Para la API usa el host de la URL **interna** y el usuario `sobusa_alertas_lector`. El tablero no recibe ninguna credencial de PostgreSQL.

Consulta los límites del [plan gratuito de Render](https://render.com/docs/free). El PostgreSQL gratuito expira a los 30 días y los servicios web pueden suspenderse por inactividad o cuota. Para evitar cargos de excedentes, verifica la configuración de facturación y las cuotas antes de publicar.

## Cargar exclusivamente el CSV autorizado

La bandeja y el endpoint de consulta son públicos cuando se publican como servicios web. Revisa autorización de cada fila y también de los campos de texto. El CSV debe tener la cabecera de `db/cabecera_csv_autorizado.txt`. Quita nombres y otros identificadores personales, historial, metadatos internos y texto que no se haya aprobado. Usa fechas ISO con zona horaria. El ZIP carece de datos.

Desde una sesión `psql` administrativa conectada con TLS **a la base nueva**, ejecuta `db/01_tabla.sql` con `\i`. Crea el usuario con `CREATE ROLE sobusa_alertas_lector LOGIN;` y asígnale la contraseña con `\password sobusa_alertas_lector`. Ejecuta `db/02_permisos_lector.sql` con `\i`. Importa solo el CSV aprobado usando este comando de `psql` en una sola línea:

```sql
\copy public.alertas_publicas (alert_id,codigo,severidad,estado,idruta,franja_inicio,horizonte_min,valor_observado,valor_esperado,umbral,puntuacion_anomalia,explicacion,recomendacion) FROM '/ruta/alertas_autorizadas.csv' WITH (FORMAT csv, HEADER true)
```

El lector recibe SELECT en la proyección. El arranque de la API comprueba los permisos en sus consultas. Mantén la carga separada del arranque de los servicios.

## Aceptación desde Internet

1. Comprueba HTTP 200 en `https://<servicio-api>.onrender.com/healthz`.
2. Abre `https://<servicio-api>.onrender.com/api/v1/alerts?idruta=16&estado=abierta&pagina=1&tamano=50`. Guarda `paginacion.total` y los IDs de `datos` en orden.
3. Abre el tablero público con **Ruta 16 + Abierta**. Compara el total, los IDs y el contenido visible con la misma página del JSON. Una respuesta vacía es coherente solo si el tablero también muestra cero.
4. Verifica que un PATCH a la ruta de alertas no esté disponible. La API de consulta no expone acciones de operación.

Referencias de Render. [FastAPI](https://render.com/docs/deploy-fastapi), [comandos de despliegue](https://render.com/docs/deploys) y [plan Free](https://render.com/docs/free).
