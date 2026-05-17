"""Pentair coordinator."""

from __future__ import annotations

from datetime import timedelta
import json
import logging
from typing import Any
from urllib.parse import urljoin

from botocore.awsrequest import AWSRequest
from deepdiff import DeepDiff
from pypentair import Pentair
import requests

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
UPDATE_INTERVAL = 30

BASE_URL = "https://api.pentair.cloud/"
DEVICE2_STATUS_PATH = "device2/device2-service/user/device"

def _signed_pentair_request(
    client: Any,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send a signed Pentair Cloud request."""
    url = urljoin(BASE_URL, path)
    body = json.dumps(payload) if payload is not None else None

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

def _get_device2_status(client: Any, device_id: str) -> dict[str, Any] | None:
    """Fetch fresher IF31 pump status from Pentair's device2 endpoint."""
    response = _signed_pentair_request(
        client,
        "POST",
        DEVICE2_STATUS_PATH,
        {"deviceIds": [device_id]},
    )

    for device in response.get("response", {}).get("data", []):
        if device.get("deviceId") == device_id:
            return device

    return None

class PentairDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    def __init__(
        self, hass: HomeAssistant, config_entry: ConfigEntry, client: Pentair
    ) -> None:
        """Initialize."""
        self.api = client
        self.devices: dict[str, list[dict[str, Any]]] = {}
        self.device_coordinators: list[PentairDeviceDataUpdateCoordinator] = []

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

    def get_device(self, device_id: str) -> dict | None:
        """Get device by id."""
        return next(
            (
                device
                for device in self.devices.get("data", [])
                if device["deviceId"] == device_id
            ),
            None,
        )

    def get_devices(self, device_type: str | None = None) -> list[dict]:
        """Get devices optionally filtered by their type."""
        return [
            device
            for device in self.devices.get("data", [])
            if device_type is None or device["deviceType"] == device_type
        ]

    async def _async_update_data(self):
        """Update data via library, refresh token if necessary."""
        try:
            if devices := await self.hass.async_add_executor_job(self.api.get_devices):
                diff = DeepDiff(
                    self.devices,
                    devices,
                    ignore_order=True,
                    report_repetition=True,
                    verbose_level=2,
                )
                _LOGGER.debug("Devices updated: %s", diff if diff else "no changes")
                self.devices = devices
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.exception("Unknown exception while updating Pentair data: %s", err)
            raise UpdateFailed(err) from err
        return self.devices


class PentairDeviceDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the device endpoint."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        client: Pentair,
        device_id: str,
    ) -> None:
        """Initialize."""
        self.api = client
        self.device_id = device_id

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

    def get_device_data(self) -> dict | None:
        """Get the device data."""
        if self.data and (data := self.data.get("data")):
            return data
        return None

    async def _async_update_data(self):
        """Update data via library, refresh token if necessary."""
        try:
            if device := await self.hass.async_add_executor_job(
                _get_device2_status, self.api, self.device_id
            ):
                diff = DeepDiff(
                    self.data,
                    device,
                    ignore_order=True,
                    report_repetition=True,
                    verbose_level=2,
                )
                _LOGGER.debug(
                    "Device %s updated: %s",
                    self.device_id,
                    diff if diff else "no changes",
                )
                return device
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.exception("Unknown exception while updating Pentair device data: %s", err)
            raise UpdateFailed(err) from err
        else:
            return None
