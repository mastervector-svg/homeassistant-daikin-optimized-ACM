"""Config flow for Daikin ACM — UDP scan with multi-select checkboxes."""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import time
from typing import Any
from urllib.parse import unquote

from pydaikin.daikin_base import Appliance
from pydaikin.factory import DaikinFactory
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PASSWORD, CONF_UUID
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import DOMAIN, KEY_MAC, TIMEOUT
from .provisioning import get_basic_info, parse_daikin_response

_LOGGER = logging.getLogger(__name__)


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    except ssl.SSLError:
        pass
    return ctx


def _decode(s: str) -> str:
    try:
        return unquote(s)
    except Exception:
        return s


def _discover() -> list[dict]:
    """UDP broadcast discovery — synchronous, runs in executor."""
    devices: list[dict] = []
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
        seen: set[str] = set()
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
    """Config flow — scan discovers adapters, user picks via checkboxes."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered: list[dict] = []
        self._selected: list[dict] = []
        self._add_index: int = 0

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Entry point — choose scan or manual."""
        if user_input is not None:
            if user_input.get("action") == "manual":
                return await self.async_step_manual()
            return await self.async_step_pick()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("action", default="scan"): vol.In(
                        {
                            "scan": "Scan network for Daikin adapters",
                            "manual": "Enter IP address manually",
                        }
                    ),
                }
            ),
        )

    async def async_step_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Scan and show multi-select checkboxes for discovered adapters."""
        if user_input is not None:
            # User submitted their selection
            chosen = user_input.get("adapters", [])
            if not chosen:
                return self.async_abort(reason="no_selection")

            # Build list of selected devices
            dev_map = {d["ip"]: d for d in self._discovered}
            self._selected = [dev_map[ip] for ip in chosen if ip in dev_map]
            self._add_index = 0

            if not self._selected:
                return self.async_abort(reason="no_selection")

            # Start adding them one by one
            return await self._add_next()

        # Perform scan
        discovered = await self.hass.async_add_executor_job(_discover)

        # Filter out already-configured MACs
        configured_macs: set[str] = set()
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            mac = entry.data.get(KEY_MAC, "")
            if mac:
                configured_macs.add(mac.upper().replace(":", ""))

        new_devs = [
            d
            for d in discovered
            if d.get("mac", "").upper() not in configured_macs
        ]
        self._discovered = new_devs

        if not new_devs:
            if discovered:
                return self.async_abort(reason="all_configured")
            return self.async_abort(reason="no_devices_found")

        # Build multi-select options: ip -> label
        options: dict[str, str] = {}
        for d in new_devs:
            name = _decode(d.get("name", d["ip"]))
            ver = d.get("ver", "?").replace("_", ".")
            mac = d.get("mac", "?")
            label = f"{name} ({d['ip']}) — FW {ver}, MAC {mac}"
            options[d["ip"]] = label

        # Default: all checked
        default_selected = list(options.keys())

        return self.async_show_form(
            step_id="pick",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "adapters", default=default_selected
                    ): vol.All(
                        [vol.In(options)],
                    ),
                }
            ),
            description_placeholders={
                "count": str(len(new_devs)),
            },
        )

    async def _add_next(self) -> ConfigFlowResult:
        """Add the next adapter from self._selected."""
        if self._add_index >= len(self._selected):
            # All done — the first one created the entry, rest via auto_add
            return self.async_abort(reason="all_configured")

        dev = self._selected[self._add_index]
        self._add_index += 1
        host = dev["ip"]
        mac = dev.get("mac", "")

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
            _LOGGER.warning("Cannot connect to %s, skipping", host)
            # Schedule remaining as auto_add, then abort this flow
            self._schedule_remaining()
            return self.async_abort(reason="cannot_connect")

        name = _decode(dev.get("name", host))

        # Schedule remaining adapters as separate flows
        self._schedule_remaining()

        return self.async_create_entry(
            title=name,
            data={
                CONF_HOST: host,
                KEY_MAC: device.mac,
                CONF_API_KEY: None,
                CONF_UUID: None,
                CONF_PASSWORD: None,
            },
        )

    def _schedule_remaining(self) -> None:
        """Schedule auto_add flows for remaining selected adapters."""
        for extra in self._selected[self._add_index :]:
            self.hass.async_create_task(
                self.hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={"source": "auto_add"},
                    data={"host": extra["ip"]},
                )
            )

    async def async_step_auto_add(
        self, discovery_info: dict[str, Any]
    ) -> ConfigFlowResult:
        """Auto-add triggered from multi-select for additional adapters."""
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

            ssl_context = await self.hass.async_add_executor_job(_ssl_ctx)
            async with asyncio.timeout(TIMEOUT):
                device: Appliance = await DaikinFactory(
                    discovery_info.host,
                    async_get_clientsession(self.hass),
                    ssl_context=ssl_context,
                )

            return self.async_create_entry(
                title=_decode(info.get("name", discovery_info.host)),
                data={
                    CONF_HOST: discovery_info.host,
                    KEY_MAC: device.mac,
                    CONF_API_KEY: None,
                    CONF_UUID: None,
                    CONF_PASSWORD: None,
                },
            )
        except Exception:
            return self.async_abort(reason="cannot_connect")
