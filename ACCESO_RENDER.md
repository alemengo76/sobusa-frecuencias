# Acceso a la bandeja de SOBUSA en Render

## Configuración sin contratar servicios

Usa los dos Web Services y el PostgreSQL en plan Free ya previstos. El login usa Google OpenID Connect, con un cliente OAuth de tipo aplicación web. Si el proyecto OAuth está en modo de prueba y Google exige registrar usuarios de prueba para tu configuración, agrega también allí a los usuarios autorizados. Configura los correos exactos en el tablero, incluso si tu organización ya limita quién puede autenticarse con el proveedor.

Necesitas definir:

1. La URL HTTPS definitiva del tablero y la de la API. Registra en el cliente OAuth la URI exacta `https://<tablero>.onrender.com/oauth2callback`.
2. La lista de correos autorizados, separados por comas en `SOBUSA_ALLOWED_EMAILS`, solo en el tablero.
3. Un ID y un secreto de cliente de Google, y dos secretos aleatorios independientes: uno para la cookie de Streamlit y otro para la comunicación del tablero con FastAPI. Genera cada secreto localmente con `python -c "import secrets; print(secrets.token_urlsafe(32))"` y cópialo directamente a Render. Evita enviar los valores por chat o guardarlos en Git.
4. La URL PostgreSQL del rol lector y el CSV con las filas y campos autorizados.

### API

Conserva `DATABASE_URL` y `PYTHON_VERSION`. Añade `SOBUSA_API_TOKEN` en Environment. La API responde 401 a una consulta o exportación con credencial ausente o incorrecta y 503 si el servidor carece de una credencial válida. `/healthz` es público y solo informa disponibilidad.

### Tablero

Conserva `API_URL=https://<api>.onrender.com/api/v1` y `PYTHON_VERSION`. Añade `SOBUSA_ALLOWED_EMAILS` y el mismo `SOBUSA_API_TOKEN` de la API. El tablero utiliza ese token en el servidor. El navegador recibe solo los datos y la descarga de su sesión autorizada.

En Environment → Secret Files agrega `secrets.toml`. Llena los marcadores en Render, sin subir el archivo a Git:

```toml
[auth]
redirect_uri = "https://<tablero>.onrender.com/oauth2callback"
cookie_secret = "<secreto aleatorio independiente>"
client_id = "<ID de la aplicación web>"
client_secret = "<secreto del cliente Google>"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"
```

El tablero comprueba en cada ejecución que el correo de Google esté verificado, que la identidad no haya expirado y que el correo coincida con la lista. La descarga CSV está disponible para quienes pasan los mismos controles. Incluye toda la proyección autorizada, independientemente del filtro. Si el CSV completo supera 8 MiB, la API devuelve un error y permite usar la exportación local sin ese límite.

Si la versión anterior de la rama ya sirve datos, restringe primero el endpoint API actual desplegando esta versión con el token. Mientras se coordinan ambos despliegues, el tablero anterior podría recibir 401. No cargues nuevos datos hasta comprobar el rechazo del acceso anónimo y de un correo fuera de la lista.

## Exportación local

El exportador trabaja con un CSV de entrada que ya fue aprobado para compartir. Usa la cabecera de `db/cabecera_csv_autorizado.txt`. No lee la base de trabajo, archivos de respaldo ni Extreme. Genera un archivo nuevo con los campos de la bandeja, comprueba IDs únicos y escapa texto que Excel podría interpretar como fórmula.

```powershell
python exportar_csv_autorizado.py --csv-autorizado 'C:\ruta\alertas_aprobadas.csv' --salida 'C:\ruta\sobusa_alertas_autorizadas.csv'
```

## Prueba de despliegue

- Sin sesión, abrir el tablero debe mostrar el botón de login sin datos.
- Con un correo autenticado fuera de la lista, el tablero debe denegar la consulta y la exportación.
- Una petición directa sin token a `/api/v1/alerts` y a `/api/v1/alerts/export` debe responder 401.
- Con un correo de la lista, abrir **Ruta 16 + Abierta** y cotejar el total e IDs con la API desde un servidor autorizado. El CSV debe contener todos los IDs aprobados aunque el filtro esté activo.
- La API usa la base separada en solo lectura. El programador y los envíos se mantienen apagados.

[Autenticación de Streamlit](https://docs.streamlit.io/develop/api-reference/user/st.login) · [Archivos secretos de Render](https://render.com/docs/configure-environment-variables)
