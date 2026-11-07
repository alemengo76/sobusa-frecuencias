-- E01/E07: copia local de los viajes de progvehiculos (solo las columnas que usa el FE 3.1).
-- La llena la ingesta incremental (misma marca de agua, mismos días releídos completos), así el
-- gradient boosting calcula sus variables y se reentrena desde la base local, sin volver a
-- Extreme en cada solicitud. Sin datos personales (no hay conductor ni placa).
SET search_path TO ai_frecuencias;

CREATE TABLE viaje_fuente (
    idprogramacion   bigint PRIMARY KEY,
    idruta           integer NOT NULL,
    fechasalida      timestamp NOT NULL,
    fechainicio      timestamp,
    fechafin         timestamp,
    finalizado       text,
    cancelada        text,
    numeropasajeros  double precision,
    numerobajadas    double precision,
    lote             text NOT NULL,
    cargado_en       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_viaje_fuente_ruta_salida ON viaje_fuente (idruta, fechasalida);
COMMENT ON TABLE viaje_fuente IS
    'Copia de progvehiculos (rutas 2, 16, 21) tal como viene de la fuente; las reglas comun-1.5 se aplican al leer';
