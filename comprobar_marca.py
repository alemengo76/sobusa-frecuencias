from app.db.conexion import leer, consultar_resultados
from app.core.config import obtener_config
from sqlalchemy import text
from datetime import timedelta

esquema = obtener_config().esquema_fuente

with consultar_resultados() as c:
    marca = c.execute(text(
        "SELECT ultima_actualizacion FROM watermark "
        "WHERE tabla_origen = 'progvehiculos'"
    )).scalar()

corte = marca - timedelta(days=3)

sql = f"""
SELECT
    fechasalida::date AS dia,
    MAX(COALESCE(fecha_fin_liquidacion, fechafin)) AS ultima_marca,
    COUNT(*) AS viajes
FROM {esquema}.progvehiculos
WHERE idruta = ANY(:r)
GROUP BY fechasalida::date
HAVING MAX(COALESCE(fecha_fin_liquidacion, fechafin)) <= :corte
ORDER BY dia
"""

with leer() as c:
    filas = c.execute(
        text(sql),
        {"corte": corte, "r": [2, 16, 21]}
    ).mappings().all()

for fila in filas:
    print(dict(fila))