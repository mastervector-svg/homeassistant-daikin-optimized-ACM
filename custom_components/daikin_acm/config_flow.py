"""Config flow for Daikin ACM — auto-discovery, bulk add."""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import time
from typing import Any
from urllib.parse import unquote, quote
from uuid import uuid4

import aiohttp
from pydaikin.daikin_base import Appliance
from pydaikin.factory import DaikinFactory
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PASSWORD, CONF_UUID
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import DOMAIN, KEY_MAC, TIMEOUT, CONF_SPW, CONF_ADAPTER_KEY
from .provisioning import get_basic_info, get_spw, check_firmware_safety, parse_daikin_response

_LOGGER = logging.getLogger(__name__)


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.set_ciphers('DEFAULT:@SECLEVEL=0')
    except ssl.SSLError:
        pass
    return ctx


def _decode(s: str) -> str:
    try:
        return unquote(s)
    except Exception:
        return s


def _discover() -> list[dict]:
    """UDP broadcast discovery — synchronous."""
    devices = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(3.0)
    try:
        sock.bind(("", 30000))
    except OSError:
        sock.bind(("", 0))
    try:
        sock.sendto(b"DAIKIN_UDP/common/basic_info", ("<broadcast>", 30050))
        seen = set()
        end = time.time() + 3.0
        while time.time() < end:
            try:
                data, addr = sock.recvfrom(4096)
                ip = addr[0]
                if ip in seen:
                    continue
                seen.add(ip)
                info = parse_daikin_response(data.decode("utf-8", errors="ignore"))
                info["ip"] = ip
                devices.append(info)
            except socket.timeout:
                break
    finally:
        sock.close()
    return devices


class FlowHandler(ConfigFlow, domain=DOMAIN):
    """Config flow — scan adds all at once."""

    VERSION = 1

    def __init__(self) -> None:
        self.host: str | None = None
        self.selected_device: dict | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Entry point — scan or manual."""
        if user_input is not None:
            if user_input.get("action") == "manual":
                return await self.async_step_manual()
            return await self.async_step_scan()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("action", default="scan"): vol.In({
                    "scan": "Scan and add all Daikin adapters",
                    "manual": "Enter IP address manually",
                }),
            }),
        )

    async def async_step_scan(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Scan network → add ALL new adapters at once → done."""
        discovered = await self.hass.async_add_executor_job(_discover)

        # Filter already configured
        configured_macs = set()
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            mac = entry.data.get(KEY_MAC, "")
            if mac:
                configured_macs.add(mac.upper().replace(":", ""))

        new_devs = [d for d in discovered if d.get("mac", "").upper() not in configured_macs]

        if not new_devs:
            if discovered:
                return self.async_abort(reason="all_configured")
            return self.async_abort(reason="no_devices_found")

        # Add the FIRST new adapter (HA config flow can only create one entry per flow)
        # But we'll auto-trigger additional flows for the rest
        dev = new_devs[0]
        self.host = dev["ip"]
        self.selected_device = dev

        mac = dev.get("mac", "")
        await self.async_set_unique_id(mac)
        self._abort_if_unique_id_configured()

        # Connect via pydaikin
        ssl_context = await self.hass.async_add_executor_job(_ssl_ctx)
        try:
            async with asyncio.timeout(TIMEOUT):
                device: Appliance = await DaikinFactory(
                    self.host,
                    async_get_clientsession(self.hass),
                    ssl_context=ssl_context,
                )
        except Exception:
            return self.async_abort(reason="cannot_connect")

        name = _decode(dev.get("name", self.host))

        # Schedule adding remaining adapters
        if len(new_devs) > 1:
            for extra in new_devs[1:]:
                self.hass.async_create_task(
                    self.hass.config_entries.flow.async_init(
                        DOMAIN,
                        context={"source": "auto_add"},
                        data={"host": extra["ip"]},
                    )
                )

        return self.async_create_entry(
            title=name,
            data={
                CONF_HOST: self.host,
                KEY_MAC: device.mac,
                CONF_API_KEY: None,
                CONF_UUID: None,
                CONF_PASSWORD: None,
                CONF_SPW: None,
                CONF_ADAPTER_KEY: None,
            },
        )

    async def async_step_auto_add(
        self, discovery_info: dict[str, Any]
    ) -> ConfigFlowResult:
        """Auto-add triggered from scan for additional adapters."""
        host = discovery_info["host"]

        try:
            info = await get_basic_info(None, host)
        except Exception:
            return self.async_abort(reason="cannot_connect")

        mac = info.get("mac", "")
        if not mac:
            return self.async_abort(reason="cannot_connect")

        await self.async_set_unique_id(mac)
        self._abort_if_unique_id_configured()

        ssl_context = await self.hass.async_add_executor_job(_ssl_ctx)
        try:
            async with asyncio.timeout(TIMEOUT):
                device: Appliance = await DaikinFactory(
                    host,
                    async_get_clientsession(self.hass),
                    ssl_context=ssl_context,
                )
        except Exception:
            return self.async_abort(reason="cannot_connect")

        name = _decode(info.get("name", host))

        return self.async_create_entry(
            title=name,
            data={
                CONF_HOST: host,
                KEY_MAC: device.mac,
                CONF_API_KEY: None,
                CONF_UUID: None,
                CONF_PASSWORD: None,
                CONF_SPW: None,
                CONF_ADAPTER_KEY: None,
            },
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manual IP entry."""
        if user_input is not None:
            host = user_input[CONF_HOST]
            try:
                info = await get_basic_info(None, host)
            except Exception:
                return self.async_show_form(
                    step_id="manual",
                    data_schema=vol.Schema({vol.Required(CONF_HOST): str}),
                    errors={"base": "cannot_connect"},
                )

            mac = info.get("mac", "")
            if mac:
                await self.async_set_unique_id(mac)
                self._abort_if_unique_id_configured()

            ssl_context = await self.hass.async_add_executor_job(_ssl_ctx)
            try:
                async with asyncio.timeout(TIMEOUT):
                    device: Appliance = await DaikinFactory(
                        host,
                        async_get_clientsession(self.hass),
                        ssl_context=ssl_context,
                    )
            except Exception:
                return self.async_show_form(
                    step_id="manual",
                    data_schema=vol.Schema({vol.Required(CONF_HOST): str}),
                    errors={"base": "cannot_connect"},
                )

            return self.async_create_entry(
                title=_decode(info.get("name", host)),
                data={
                    CONF_HOST: host,
                    KEY_MAC: device.mac,
                    CONF_API_KEY: None,
                    CONF_UUID: None,
                    CONF_PASSWORD: None,
                    CONF_SPW: None,
                    CONF_ADAPTER_KEY: None,
                },
            )

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({vol.Required(CONF_HOST): str}),
        )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Zeroconf discovery."""
        try:
            info = await get_basic_info(None, discovery_info.host)
            mac = info.get("mac", "")
            if not mac:
                return self.async_abort(reason="cannot_connect")
            await self.async_set_unique_id(mac)
            self._abort_if_unique_id_configured()
            self.host = discovery_info.host
            self.selected_device = info

            ssl_context = await self.hass.async_add_executor_job(_ssl_ctx)
            async with asyncio.timeout(TIMEOUT):
                device: Appliance = await DaikinFactory(
                    self.host,
                    async_get_clientsession(self.hass),
                    ssl_context=ssl_context,
                )

            return self.async_create_entry(
                title=_decode(info.get("name", self.host)),
                data={
                    CONF_HOST: self.host,
                    KEY_MAC: device.mac,
                    CONF_API_KEY: None,
                    CONF_UUID: None,
                    CONF_PASSWORD: None,
                    CONF_SPW: None,
                    CONF_ADAPTER_KEY: None,
                },
            )
        except Exception:
            return self.async_abort(reason="cannot_connect")
