# Correcciones al cuaderno `feature_engineering.ipynb`

No se editó el cuaderno porque los datos (`salidas_extraccion/`, `herramientas/`) no están en
este computador y un cuaderno modificado sin volver a ejecutarse quedaría inconsistente. Estos
son los cambios para que **la persona A** los aplique y vuelva a ejecutar.

## 1. Configuración (`CFG`, primera celda de código)

| Clave | Antes | Después | Por qué |
|---|---|---|---|
| `rezagos_min` | `[15, 60, 1440, 10080]` | `[1440, 10080]` | Con el contrato D+1, los datos de hace 15 o 60 min no existen al predecir. |
| `entrenamiento_habilitado` | `False` | `True` | La especificación exige línea base y modelo candidato. |
| `disponibilidad_resultado` | `'no acreditada'` | `'D+1, supuesto operativo (carga nocturna)'` | Es la decisión documentada en `DECISIONES.md`. |
| `version_reglas` | `'sobusa-fe-2.0'` | `'sobusa-fe-2.1-d1'` | Para que la trazabilidad muestre el cambio. |

## 2. Historiales (sección 6.3)

`historial_retro` calcula sus estadísticas sobre `[t − 24 h, t)`, ventana que incluye viajes del
mismo día de t: **hay fuga temporal**. No hay que borrarlo (queda como evidencia de la auditoría);
para el modelo se usa `app/ml/features.py::features_d1`, que cierra todo a la medianoche:

```python
import sys; sys.path.insert(0, r"..\sobusa-prototipo")
from app.ml.features import features_d1
feats = (A.set_index("origen").groupby("id_ruta", group_keys=True)
           ["pax_elegibles_completo_snapshot"].apply(features_d1))
```

Agreguen esas columnas a `predictores_autorizados.json`.

## 3. Festivos

Agregar `es_festivo` con la librería `holidays` (Colombia). El calendario de
`estacionalidades_anio` es del proceso `cootransoriente`, no de Sobusa.
`app/ml/features.py::calendario` ya lo hace.

## 4. Cuarentena de extremos

Antes de agregar por franja, cruzar cada viaje con su capacidad (`app/calidad/capacidad.py`) y
mandar a cuarentena los que tengan `pasajeros > 3 × plazas` (regla `CAL-003` en
`app/calidad/cuarentena.py`). Así el viaje de 97.199 pasajeros de la ruta 16 deja de dañar el RMSE,
sin tocar el original.

## 5. Después de re-ejecutar

```bash
python -m scripts.entrenar_y_evaluar --franjas "<ruta>/salidas_feature_engineering/auditoria_franjas_conjunta.parquet"
```

Ese script entrena y compara las líneas base, el método Extreme y el gradient boosting.
