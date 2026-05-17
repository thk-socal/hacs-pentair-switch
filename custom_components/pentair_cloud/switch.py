"""Pentair Home pump-program switches."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import Any
from urllib.parse import urljoin

from botocore.awsrequest import AWSRequest
import requests

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import PentairConfigEntry
from .const import DOMAIN
from .coordinator import PentairDeviceDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://api.pentair.cloud/"
DEVICE_SERVICE_PATH = "device/device-service/user/device/{device_id}"
PROGRAM_RANGE = range(1, 9)


@dataclass(frozen=True)
class PumpProgram:
    """A pump program defined on an IntelliFlo 3 device."""

    program_id: int
    name: str
    program_type: int | None

    @property
    def start_value(self) -> str:
        """Return the Pentair API value used to start this program."""
        return "3"

    @property
    def stop_value(self) -> str:
        """Return the Pentair API value used to stop this program."""
        return "2"


def _field_value(fields: dict[str, Any], key: str, default: Any = None) -> Any:
    """Return a Pentair field value from either raw or wrapped field objects."""
    value = fields.get(key, default)
    if isinstance(value, dict):
        return value.get("value", default)
    return value


def _active_program_id(fields: dict[str, Any]) -> int | None:
    """Return the active program id using Pentair's zero-indexed s14 field."""
    raw = _field_value(fields, "s14")
    try:
        return int(raw) + 1
    except (TypeError, ValueError):
        return None


def _programs_from_device_data(data: dict[str, Any]) -> list[PumpProgram]:
    """Build the list of active/configured programs for an IF31 pump."""
    fields = data.get("fields", {})
    programs: list[PumpProgram] = []

    for program_id in PROGRAM_RANGE:
        enabled = str(_field_value(fields, f"zp{program_id}e13", "0")) == "1"
        if not enabled:
            continue

        name = str(
            _field_value(fields, f"zp{program_id}e2", f"Program {program_id}")
        ).strip()

        raw_type = _field_value(fields, f"zp{program_id}e5")
        try:
            program_type = int(raw_type)
        except (TypeError, ValueError):
            program_type = None

        programs.append(
            PumpProgram(
                program_id=program_id,
                name=name or f"Program {program_id}",
                program_type=program_type,
            )
        )

    return programs


def _signed_pentair_request(
    client: Any,
    method: str,
    path: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Send a signed Pentair Cloud request using the existing pypentair client auth."""
    url = urljoin(BASE_URL, path)
    body = json.dumps(payload)

    request = AWSRequest(
        method=method.upper(),
        url=url,
        data=body,
        headers={
            "x-amz-id-token": client.id_token,
            "content-type": "application/json; charset=UTF-8",
            "user-agent": "aws-amplify/4.3.10 react-native",
        },
    )

    client.get_auth().add_auth(request)
    prepared = request.prepare()

    response = requests.request(
        method=method.upper(),
        url=prepared.url,
        data=body,
        headers=dict(prepared.headers),
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


def _set_program(
    client: Any,
    device_id: str,
    program: PumpProgram,
    value: str,
) -> None:
    """Set a pump program command field."""
    path = DEVICE_SERVICE_PATH.format(device_id=device_id)

    response = _signed_pentair_request(
        client,
        "PUT",
        path,
        {"payload": {f"zp{program.program_id}e10": value}},
    )

    if response.get("data", {}).get("code") != "set_device_success":
        raise RuntimeError(
            f"Unexpected Pentair response while controlling program: {response}"
        )


def _set_last_active_program(client: Any, device_id: str, value: str) -> None:
    """Mirror the Pentair Home app's p2 update after program start/stop."""
    path = DEVICE_SERVICE_PATH.format(device_id=device_id)
    _signed_pentair_request(client, "PUT", path, {"payload": {"p2": value}})


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: PentairConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Pentair pump-program switches."""
    root_coordinator = config_entry.runtime_data
    entities: list[PentairPumpProgramSwitch] = []

    for device_coordinator in root_coordinator.device_coordinators:
        data = device_coordinator.get_device_data()

        if not data or data.get("deviceType") != "IF31":
            continue

        device_id = data["deviceId"]

        for program in _programs_from_device_data(data):
            entities.append(
                PentairPumpProgramSwitch(
                    coordinator=device_coordinator,
                    config_entry=config_entry,
                    device_id=device_id,
                    program=program,
                )
            )

    if entities:
        async_add_entities(entities)


class PentairPumpProgramSwitch(
    CoordinatorEntity[PentairDeviceDataUpdateCoordinator],
    SwitchEntity,
):
    """Switch entity for an IntelliFlo 3 pump program."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PentairDeviceDataUpdateCoordinator,
        config_entry: PentairConfigEntry,
        device_id: str,
        program: PumpProgram,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)

        self._config_entry = config_entry
        self._device_id = device_id
        self._program = program
        self._optimistic_is_on: bool | None = None

        self._attr_name = f"P{program.program_id} / {program.name}"
        self._attr_unique_id = f"{device_id}-program-{program.program_id}"

        device = self._device_data or {}
        product_info = device.get("productInfo", {})
        model = product_info.get("model")

        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_id)},
            "manufacturer": product_info.get("maker", "Pentair"),
            "model": device.get("pname", "Pentair pump")
            + (f" ({model})" if model else ""),
            "name": product_info.get("nickName", "Pentair pump"),
            "sw_version": device.get("fwVersion"),
        }

    @property
    def _device_data(self) -> dict[str, Any] | None:
        """Return the current pump device data."""
        return self.coordinator.get_device_data()

    def _confirmed_is_on(self) -> bool | None:
        """Return confirmed running state from coordinator data."""
        data = self._device_data
        if not data:
            return None

        active = _active_program_id(data.get("fields", {}))
        return active == self._program.program_id

    @property
    def is_on(self) -> bool | None:
        """Return whether this pump program is currently running."""
        confirmed = self._confirmed_is_on()

        if self._optimistic_is_on is not None:
            if confirmed == self._optimistic_is_on:
                self._optimistic_is_on = None
                return confirmed
            return self._optimistic_is_on

        return confirmed

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start this pump program."""
        client = self.coordinator.api

        await self.hass.async_add_executor_job(
            _set_program,
            client,
            self._device_id,
            self._program,
            self._program.start_value,
        )

        await self.hass.async_add_executor_job(
            _set_last_active_program,
            client,
            self._device_id,
            "99",
        )

        self._optimistic_is_on = True
        self.async_write_ha_state()

        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop this pump program."""
        client = self.coordinator.api

        await self.hass.async_add_executor_job(
            _set_program,
            client,
            self._device_id,
            self._program,
            self._program.stop_value,
        )

        await self.hass.async_add_executor_job(
            _set_last_active_program,
            client,
            self._device_id,
            str(self._program.program_id - 1),
        )

        self._optimistic_is_on = False
        self.async_write_ha_state()

        await self.coordinator.async_request_refresh()