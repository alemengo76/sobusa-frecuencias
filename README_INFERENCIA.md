# SOBUSA v1.0 | Integración de inferencia

Funciona en Windows AMD64 con Python 3.12 y CPU. Extraiga en una ruta corta y disponga de espacio para el ZIP, las ruedas y dos entornos. Desde la raíz extraída, con PowerShell:

```powershell
& ./dependencias/instalar-local.ps1 -Python python
```

El instalador verifica los hashes de los dos locks. Para el worker, suministre un directorio de ruedas autorizadas que contenga la versión CPU de PyTorch que consta en el lock. Use `-Ruedas <directorio>` con un inventario completo para instalar sin red. El Python base y, si el equipo lo requiere, el runtime de Visual C++ x64 se instalan por separado. El backend y el worker tienen versiones diferentes de scikit-learn.

La API empresarial y las pruebas con un proveedor simulado pueden ejecutarse con el código público. Para ejecutar el ensamble real, monte localmente la parte privada de `sobusa_v1_runtime/` desde la distribución autorizada, incluidos RRAP, FE, cargador, adaptador e interfaz. Los tres ejemplos usan esa misma interfaz. Aporte los cinco estados RRAP y los cinco boosters LightGBM, el horario y el manifiesto congelado mediante `SOBUSA_ARTEFACTOS_DIR`. Los ejemplos sintéticos requieren los modelos y el runtime, sin requerir históricos.

## Ejemplos con viajes sintéticos

```powershell
& ./.venv-worker/Scripts/python.exe -m sobusa_v1_runtime --modo sintetico --fecha 2025-05-01 --rutas 2 16 21 --horizontes 30 60 120
& ./.venv-worker/Scripts/python.exe ejemplos/verificar_notebook.py
```

Desde otro script, con el intérprete del worker y la raíz del paquete en la ruta de importación:

```python
from sobusa_v1_runtime.interfaz import predecir

resultado = predecir("2025-05-01", [2, 16, 21], [30, 60, 120], modo="sintetico")
print(resultado["filas"][:3])
```

El comando del notebook ejecuta `ejemplos/inferencia_minima.ipynb` en un kernel Jupyter real. Para abrirlo desde un Jupyter externo, registre el kernel local con `./.venv-worker/Scripts/python.exe -m ipykernel install --user --name sobusa-v1-worker` y seleccione ese kernel.

Para FastAPI, abra una consola de demostración sin DSN ni variables de conexión empresarial. Conserve el token generado en la consola y arranque Uvicorn en la misma:

```powershell
$env:PYTHON_DOTENV_DISABLED = '1'
Remove-Item Env:SOBUSA_HISTORICO_DIR -ErrorAction SilentlyContinue
$env:SOBUSA_CANDIDATO_LOCAL = '1'
$env:SOBUSA_DATOS_SINTETICOS = '1'
$env:SOBUSA_OPERADOR_TOKEN = [guid]::NewGuid().ToString('N')
& ./.venv-backend/Scripts/python.exe -m uvicorn app.api.main:app --host 127.0.0.1 --port 8766
```

En la consola cliente, con el token de la sesión en su propia variable de entorno:

```powershell
$api = 'http://127.0.0.1:8766/api/v1'
$headers = @{ Authorization = "Bearer $env:SOBUSA_OPERADOR_TOKEN" }
$body = @{ modelo='sobusa_v1'; version='sobusa-v1.0-p25-d3-r01'; fecha='2025-05-01';
           rutas=@(2,16,21); horizontes=@(30,60,120) } | ConvertTo-Json
$run = Invoke-RestMethod "$api/candidate-runs" -Method Post -Headers $headers -ContentType 'application/json' -Body $body
Invoke-RestMethod "$api/candidate-runs/$($run.run_id)/predictions?horizonte_min=60&pagina=1&tamano=3" -Headers $headers
Invoke-RestMethod "$api/candidate-runs/$($run.run_id)/recommendations?pagina=1&tamano=3" -Headers $headers
```

El contrato común entrega claves de ruta, inicio de franja y horizonte, y los tres valores puntuales `demanda_rrap_v4`, `demanda_lightgbm_l3` y `demanda_pred`. La salida identifica versión, corte, snapshot, cobertura y modalidad. La simulación de despacho usa el punto a 60 minutos. El candidato sigue apagado por defecto. El flujo empresarial utiliza el modelo activo, incluido `gbm_fe31` cuando está registrado y activo, y conserva `baseline_dh_v1` al inicializar una base.

## Validación histórica y operación

El modo `retrospectivo` reproduce las referencias p25 para las fechas aceptadas. Requiere por separado las 59 partes Parquet y la referencia archivada, identificadas en `ORIGEN.ejecucion.json`. Indique su directorio mediante `SOBUSA_HISTORICO_DIR`, quite `SOBUSA_DATOS_SINTETICOS` y use `--modo retrospectivo` en el CLI. FastAPI rechaza configurar a la vez el histórico y la demostración sintética. La paridad histórica se comprueba en cada llamada de ese modo. La muestra sintética ejecuta las mismas reglas D−3 y carga los mismos modelos y horario congelado. Su snapshot está marcado como sintético y no produce métricas de paridad histórica.

Las fechas nuevas siguen fuera del contrato validado. Se requiere acreditar recepción, esquema y cobertura al corte y verificar inferencia fuera del periodo p25. Los intervalos están bloqueados. La persistencia empresarial, migraciones, alertas persistidas y registro de modelos requieren la comprobación con PostgreSQL autorizado. Los tests con dobles de repositorio solo comprueban el contrato de API.

El modo histórico requiere las rutas relativas y hashes del inventario que acompaña la distribución privada. Para habilitar también la simulación histórica de frecuencia configure `SOBUSA_PARAMETROS_FRECUENCIA` con el archivo privado de parámetros, organizado por ruta y tipo de día, con los campos de `ParametrosRuta` salvo `idruta`. Sin ese archivo, el histórico devuelve pronósticos y una lista de simulaciones vacía. La demostración sintética usa exclusivamente parámetros ficticios para la simulación. La API empresarial sigue usando su propio registro y sus parámetros autorizados.

Ejecute las pruebas públicas del canal con `./.venv-backend/Scripts/python.exe -m pytest -q tests/api/test_api_candidatos_publico.py`. Esta prueba simula el proveedor privado y comprueba el contrato, la selección explícita y la simulación sintética. La paridad de las tres interfaces con los diez artefactos se verificó por separado en la distribución privada.
