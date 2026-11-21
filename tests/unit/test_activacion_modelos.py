from unittest.mock import MagicMock

import pytest

from app.core.errores import Conflicto
from app.db import repositorio


def conexion_con_objetivo(estado, anterior=1):
    conexion = MagicMock()
    objetivo = MagicMock()
    objetivo.mappings.return_value.first.return_value = {"estado": estado}
    activo = MagicMock()
    activo.scalar.return_value = anterior
    conexion.execute.side_effect = [None, objetivo, activo, None, None, None, None]
    return conexion


def test_repositorio_bloquea_y_rechaza_antes_de_escribir():
    conexion = conexion_con_objetivo("rechazado")
    with pytest.raises(Conflicto, match="rechazado"):
        repositorio.activar_modelo(conexion, 9, "ana", "prueba")
    candado, objetivo = [str(x.args[0]) for x in conexion.execute.call_args_list]
    assert "pg_advisory_xact_lock" in candado
    assert "FOR UPDATE" in objetivo


@pytest.mark.parametrize("estado", ["candidato", "retirado"])
def test_repositorio_activa_elegible_y_consulta_el_activo_despues_del_candado(estado):
    conexion = conexion_con_objetivo(estado)
    assert repositorio.activar_modelo(conexion, 9, "ana", "prueba") == {
        "model_id": 9, "modelo_anterior": 1,
    }
    candado, objetivo, activo, retiro, historial_retiro, activar, historial = conexion.execute.call_args_list
    assert "pg_advisory_xact_lock" in str(candado.args[0])
    assert "FOR UPDATE" in str(objetivo.args[0]) and "FOR UPDATE" in str(activo.args[0])
    assert retiro.args[1]["m"] == historial_retiro.args[1]["m"] == 1
    assert activar.args[1]["m"] == historial.args[1]["m"] == 9
    assert historial.args[1]["antes"] == estado
