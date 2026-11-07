-- E04: esquema ai_frecuencias. Todo lo nuevo vive aquí; las tablas de Extreme son de solo lectura.
-- El esquema lo crea el administrador (docker/initdb o Extreme). CREATE SCHEMA IF NOT EXISTS
-- exige permiso CREATE sobre la base aunque el esquema exista, así que solo se crea si falta.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'ai_frecuencias') THEN
        EXECUTE 'CREATE SCHEMA ai_frecuencias';
    END IF;
END $$;
SET search_path TO ai_frecuencias;

-- Bitácora de ejecuciones (cada corrida de predicción, recomendación, alertas o entrenamiento)
CREATE TABLE execution_log (
    execution_id     uuid PRIMARY KEY,
    tipo             text NOT NULL CHECK (tipo IN ('frecuencias', 'prediccion', 'recomendacion',
                                                   'alertas', 'ingesta', 'entrenamiento')),
    estado           text NOT NULL DEFAULT 'en_curso'
                         CHECK (estado IN ('en_curso', 'exitosa', 'fallida')),
    parametros       jsonb NOT NULL DEFAULT '{}'::jsonb,
    resumen          jsonb NOT NULL DEFAULT '{}'::jsonb,
    error            text,
    correlation_id   text,
    iniciada_en      timestamptz NOT NULL DEFAULT now(),
    finalizada_en    timestamptz
);

-- Registro de modelos: trazabilidad completa del artefacto
CREATE TABLE model_registry (
    model_id          serial PRIMARY KEY,
    nombre            text NOT NULL,
    version           text NOT NULL,
    tipo              text NOT NULL CHECK (tipo IN ('baseline', 'candidato')),
    estado            text NOT NULL DEFAULT 'candidato'
                          CHECK (estado IN ('activo', 'candidato', 'rechazado', 'retirado')),
    semilla           integer,
    hiperparametros   jsonb NOT NULL DEFAULT '{}'::jsonb,
    version_datos     text NOT NULL,
    feature_version   text NOT NULL,
    metricas          jsonb NOT NULL DEFAULT '{}'::jsonb,
    artefacto_ruta    text,
    artefacto_sha256  char(64),
    creado_en         timestamptz NOT NULL DEFAULT now(),
    activado_en       timestamptz,
    activado_por      text,
    UNIQUE (nombre, version)
);
-- Solo un modelo activo a la vez
CREATE UNIQUE INDEX uq_model_activo ON model_registry ((estado)) WHERE estado = 'activo';

CREATE TABLE model_history (
    id           bigserial PRIMARY KEY,
    model_id     integer NOT NULL REFERENCES model_registry (model_id),
    estado_antes text,
    estado_nuevo text NOT NULL,
    motivo       text,
    usuario      text,
    creado_en    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE prediction (
    prediction_id      bigserial PRIMARY KEY,
    execution_id       uuid NOT NULL REFERENCES execution_log (execution_id),
    idruta             integer NOT NULL CHECK (idruta IN (2, 16, 21)),
    sentido            text NOT NULL DEFAULT 'CICLO' CHECK (sentido = 'CICLO'),
    segmento_id        text NOT NULL DEFAULT 'TOTAL',
    franja_inicio      timestamptz NOT NULL,
    franja_minutos     smallint NOT NULL DEFAULT 15,
    horizonte_min      smallint NOT NULL CHECK (horizonte_min IN (30, 60, 120)),
    demanda_pred       numeric(10, 2) NOT NULL CHECK (demanda_pred >= 0),
    limite_inferior    numeric(10, 2) NOT NULL,
    limite_superior    numeric(10, 2) NOT NULL,
    nivel_intervalo    numeric(3, 2) NOT NULL,
    definicion_tiempo  text NOT NULL DEFAULT 'salida_programada',
    model_id           integer NOT NULL REFERENCES model_registry (model_id),
    model_version      text NOT NULL,
    feature_version    text NOT NULL,
    data_cutoff        timestamptz NOT NULL,
    generated_at       timestamptz NOT NULL DEFAULT now(),
    latency_ms         integer NOT NULL,
    CHECK (limite_inferior <= demanda_pred AND demanda_pred <= limite_superior),
    UNIQUE (idruta, sentido, segmento_id, franja_inicio, horizonte_min, model_id, data_cutoff)
);
CREATE INDEX ix_prediction_ruta_franja ON prediction (idruta, franja_inicio);

-- Parámetros operativos versionados (capacidad, ocupación, ciclo, factor de carga, flota)
CREATE TABLE parametro_operativo (
    id                   serial PRIMARY KEY,
    idruta               integer NOT NULL,
    tipo_dia             text NOT NULL DEFAULT 'todos'
                             CHECK (tipo_dia IN ('todos', 'laboral', 'sabado', 'domingo_festivo')),
    capacidad_nominal    numeric(6, 1) NOT NULL CHECK (capacidad_nominal > 0),
    capacidad_fuente     text NOT NULL,
    ocupacion_objetivo   numeric(3, 2) NOT NULL CHECK (ocupacion_objetivo BETWEEN 0.65 AND 0.85),
    ciclo_min            numeric(6, 1) NOT NULL CHECK (ciclo_min > 0),
    factor_carga_max     numeric(4, 3) NOT NULL CHECK (factor_carga_max > 0 AND factor_carga_max <= 1),
    factor_carga_fuente  text NOT NULL,
    flota_tope           integer CHECK (flota_tope > 0),
    headway_minimo_min   numeric(4, 1) NOT NULL DEFAULT 1.0,
    version              text NOT NULL,
    vigente              boolean NOT NULL DEFAULT true,
    creado_en            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (idruta, tipo_dia, version)
);
CREATE UNIQUE INDEX uq_parametro_vigente ON parametro_operativo (idruta, tipo_dia) WHERE vigente;

CREATE TABLE recommendation (
    recommendation_id        bigserial PRIMARY KEY,
    execution_id             uuid NOT NULL REFERENCES execution_log (execution_id),
    prediction_id            bigint REFERENCES prediction (prediction_id),
    idruta                   integer NOT NULL,
    sentido                  text NOT NULL DEFAULT 'CICLO',
    segmento_id              text NOT NULL DEFAULT 'TOTAL',
    franja_inicio            timestamptz NOT NULL,
    horizonte_min            smallint NOT NULL,
    demanda_hora             numeric(10, 2) NOT NULL,
    base_demanda             text NOT NULL CHECK (base_demanda IN ('puntual', 'superior')),
    factor_carga_max         numeric(4, 3) NOT NULL,
    carga_max_estimada_hora  numeric(10, 2) NOT NULL,
    capacidad_nominal        numeric(6, 1) NOT NULL CHECK (capacidad_nominal > 0),
    capacidad_fuente         text NOT NULL,
    ocupacion_objetivo       numeric(3, 2) NOT NULL,
    capacidad_efectiva       numeric(8, 2) NOT NULL,
    buses_hora_recomendado   integer,
    headway_recomendado_min  numeric(6, 2),
    ciclo_min                numeric(6, 1) NOT NULL,
    vehiculos_requeridos     integer NOT NULL,
    flota_tope               integer,
    buses_hora_actual        numeric(6, 2),
    headway_actual_min       numeric(6, 2),
    delta_buses_hora         numeric(6, 2),
    es_viable                boolean NOT NULL,
    motivo_no_viable         text,
    parametros_version       text NOT NULL,
    generated_at             timestamptz NOT NULL DEFAULT now(),
    CHECK (es_viable OR motivo_no_viable IS NOT NULL),
    CHECK (NOT es_viable OR buses_hora_recomendado IS NOT NULL)
);
CREATE INDEX ix_recommendation_ruta_franja ON recommendation (idruta, franja_inicio);

-- Catálogo y umbrales versionados (modificables sin recompilar)
CREATE TABLE alert_catalog (
    codigo          text PRIMARY KEY CHECK (codigo ~ '^ALT-[0-9]{3}$'),
    nombre          text NOT NULL,
    severidad_base  text NOT NULL CHECK (severidad_base IN ('baja', 'media', 'alta')),
    recomendacion   text NOT NULL,
    evaluable       boolean NOT NULL DEFAULT true,
    nota            text
);

CREATE TABLE umbral (
    id           serial PRIMARY KEY,
    codigo       text NOT NULL REFERENCES alert_catalog (codigo),
    parametro    text NOT NULL,
    valor        numeric NOT NULL,
    version      integer NOT NULL,
    vigente      boolean NOT NULL DEFAULT true,
    creado_por   text NOT NULL DEFAULT current_user,
    creado_en    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (codigo, version)
);
CREATE UNIQUE INDEX uq_umbral_vigente ON umbral (codigo) WHERE vigente;

CREATE TABLE alert (
    alert_id             bigserial PRIMARY KEY,
    execution_id         uuid REFERENCES execution_log (execution_id),
    codigo               text NOT NULL REFERENCES alert_catalog (codigo),
    severidad            text NOT NULL CHECK (severidad IN ('baja', 'media', 'alta')),
    estado               text NOT NULL DEFAULT 'abierta'
                             CHECK (estado IN ('abierta', 'reconocida', 'asignada', 'cerrada', 'descartada')),
    idruta               integer NOT NULL,
    franja_inicio        timestamptz,
    horizonte_min        smallint,
    valor_observado      numeric,
    valor_esperado       numeric,
    umbral               numeric NOT NULL,
    umbral_version       integer,
    puntuacion_anomalia  numeric,
    explicacion          text NOT NULL,
    recomendacion        text NOT NULL,
    usuario_asignado     text,
    clave_dedup          text NOT NULL,
    creada_en            timestamptz NOT NULL DEFAULT now(),
    actualizada_en       timestamptz NOT NULL DEFAULT now()
);
-- Deduplicación: una sola alerta abierta/en gestión por clave
CREATE UNIQUE INDEX uq_alert_dedup_abierta ON alert (clave_dedup)
    WHERE estado IN ('abierta', 'reconocida', 'asignada');
CREATE INDEX ix_alert_estado ON alert (estado, severidad, creada_en DESC);

-- Bitácora de cada alerta: cambios de estado, asignaciones y comentarios
CREATE TABLE alert_history (
    id            bigserial PRIMARY KEY,
    alert_id      bigint NOT NULL REFERENCES alert (alert_id),
    accion        text NOT NULL CHECK (accion IN ('creada', 'cambio_estado', 'asignada', 'comentario')),
    estado_antes  text,
    estado_nuevo  text,
    comentario    text,
    usuario       text NOT NULL,
    creado_en     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_alert_history_alert ON alert_history (alert_id, creado_en);

CREATE TABLE notification_log (
    id          bigserial PRIMARY KEY,
    alert_id    bigint NOT NULL REFERENCES alert (alert_id),
    canal       text NOT NULL CHECK (canal IN ('correo', 'whatsapp')),
    destino     text NOT NULL,
    simulada    boolean NOT NULL DEFAULT true,
    estado      text NOT NULL,
    creado_en   timestamptz NOT NULL DEFAULT now()
);

-- Calidad de datos: errores consultables por lote, con causa y fila
CREATE TABLE data_quality_error (
    id              bigserial PRIMARY KEY,
    lote            text NOT NULL,
    tabla_origen    text NOT NULL,
    idprogramacion  bigint,
    idruta          integer,
    regla           text NOT NULL,
    causa           text NOT NULL,
    valor           text,
    reglas_version  text NOT NULL,
    creado_en       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (lote, tabla_origen, idprogramacion, regla)
);
CREATE INDEX ix_dq_lote ON data_quality_error (lote);

-- Marca de agua de la lectura incremental (ultimaactualizacion de cada tabla fuente)
CREATE TABLE watermark (
    tabla_origen         text PRIMARY KEY,
    ultima_actualizacion timestamp NOT NULL,
    filas_ultima_carga   integer NOT NULL DEFAULT 0,
    lote                 text NOT NULL,
    actualizada_en       timestamptz NOT NULL DEFAULT now()
);

-- Agregados por franja leídos de la fuente (la API consulta aquí, no las tablas de Extreme)
CREATE TABLE demanda_franja (
    idruta                integer NOT NULL,
    franja_inicio         timestamp NOT NULL,
    pasajeros             numeric(10, 2),
    programaciones        integer NOT NULL,
    programaciones_validas integer NOT NULL,
    lote                  text NOT NULL,
    PRIMARY KEY (idruta, franja_inicio)
);
