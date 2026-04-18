"""Config flow for Daikin ACM — auto-discovery wizard."""

from __future__ import annotations

import asyncio
import logging
import ssl
from typing import Any
from urllib.parse import unquote
from uuid import uuid4

from aiohttp import ClientError, web_exceptions
from pydaikin.daikin_base import Appliance
from pydaikin.discovery import Discovery
from pydaikin.exceptions import DaikinException
from pydaikin.factory import DaikinFactory
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PASSWORD, CONF_UUID
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import DOMAIN, KEY_MAC, TIMEOUT, CONF_SPW, CONF_ADAPTER_KEY
from .provisioning import (
    get_basic_info,
    get_spw,
    register_terminal,
    check_firmware_safety,
    discover_adapters,
    parse_daikin_response,
)
from .telemetry import lookup_key, generate_contribution_url

_LOGGER = logging.getLogger(__name__)


def get_daikin_ssl_context() -> ssl.SSLContext:
    """Create SSL context with legacy Daikin support."""
    ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    try:
        ssl_context.set_ciphers('DEFAULT:@SECLEVEL=0')
    except ssl.SSLError:
        pass
    return ssl_context


def _decode_daikin_name(encoded: str) -> str:
    """Decode %XX encoded Daikin device name."""
    try:
        return unquote(encoded)
    except Exception:
        return encoded


class FlowHandler(ConfigFlow, domain=DOMAIN):
    """Handle a config flow with auto-discovery."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the Daikin config flow."""
        self.host: str | None = None
        self.discovered: list[dict] = []
        self.selected_device: dict | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: Scan network or enter IP manually."""
        if user_input is not None:
            choice = user_input.get("action", "scan")
            if choice == "manual":
                return await self.async_step_manual()
            # Scan selected — discover adapters
            return await self.async_step_discovered()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("action", default="scan"): vol.In({
                    "scan": "Scan network for Daikin adapters",
                    "manual": "Enter IP address manually",
                }),
            }),
            description_placeholders={
                "title": "Daikin AC (ACM Edition)",
            },
        )

    async def async_step_discovered(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: Show discovered adapters, user picks one."""
        if user_input is not None:
            # User selected a device
            selected_ip = user_input["device"]
            for dev in self.discovered:
                if dev.get("ip") == selected_ip:
                    self.selected_device = dev
                    break
            if self.selected_device:
                self.host = selected_ip
                # Check if already configured
                mac = self.selected_device.get("mac", "")
                await self.async_set_unique_id(mac)
                self._abort_if_unique_id_configured()
                return await self.async_step_confirm()
            return self.async_abort(reason="cannot_connect")

        # Run UDP discovery
        try:
            self.discovered = await self.hass.async_add_executor_job(
                self._discover_sync
            )
        except Exception as err:
            _LOGGER.error("Discovery failed: %s", err)
            self.discovered = []

        if not self.discovered:
            return self.async_show_form(
                step_id="discovered",
                data_schema=vol.Schema({
                    vol.Required("device"): str,
                }),
                errors={"base": "no_devices_found"},
            )

        # Build selection list: "IP — Name (FW ver)"
        device_options = {}
        for dev in self.discovered:
            ip = dev.get("ip", "?")
            name = _decode_daikin_name(dev.get("name", "Unknown"))
            ver = dev.get("ver", "?").replace("_", ".")
            mac = dev.get("mac", "?")
            safety = check_firmware_safety(ver)
            warn = " !!!" if not safety["safe"] else ""
            device_options[ip] = f"{name} — {ip} (FW {ver}, MAC ...{mac[-4:]}){warn}"

        return self.async_show_form(
            step_id="discovered",
            data_schema=vol.Schema({
                vol.Required("device"): vol.In(device_options),
            }),
        )

    def _discover_sync(self) -> list[dict]:
        """Synchronous UDP discovery wrapper."""
        import socket
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
            sock.sendto(
                b"DAIKIN_UDP/common/basic_info",
                ("<broadcast>", 30050),
            )
            seen = set()
            import time
            deadline = time.time() + 3.0
            while time.time() < deadline:
                try:
                    data, addr = sock.recvfrom(4096)
                    ip = addr[0]
                    if ip in seen:
                        continue
                    seen.add(ip)
                    info = parse_daikin_response(
                        data.decode("utf-8", errors="ignore")
                    )
                    info["ip"] = ip
                    devices.append(info)
                except socket.timeout:
                    break
        finally:
            sock.close()
        return devices

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 3: Show adapter details, ask for KEY if needed, confirm."""
        dev = self.selected_device or {}
        name = _decode_daikin_name(dev.get("name", "Daikin AC"))
        ver = dev.get("ver", "?").replace("_", ".")
        mac = dev.get("mac", "")
        method = dev.get("method", "")
        lpw_flag = dev.get("lpw_flag", "0")
        safety = check_firmware_safety(ver)

        if user_input is not None:
            adapter_key = user_input.get(CONF_ADAPTER_KEY, "")

            # If adapter needs registration and key provided
            if adapter_key and method == "home only" and not dev.get("id"):
                session = async_get_clientsession(self.hass)
                ok = await register_terminal(session, self.host, adapter_key)
                if not ok:
                    return self.async_show_form(
                        step_id="confirm",
                        data_schema=self._confirm_schema(dev, safety),
                        errors={"base": "invalid_auth"},
                        description_placeholders=self._confirm_placeholders(
                            dev, safety
                        ),
                    )
                _LOGGER.info("ACM: registered terminal on %s", self.host)

            # Connect via pydaikin
            return await self._finalize(
                host=self.host,
                api_key=user_input.get(CONF_API_KEY),
                password=user_input.get(CONF_PASSWORD),
                adapter_key=adapter_key or None,
            )

        return self.async_show_form(
            step_id="confirm",
            data_schema=self._confirm_schema(dev, safety),
            description_placeholders=self._confirm_placeholders(dev, safety),
        )

    def _confirm_schema(self, dev: dict, safety: dict) -> vol.Schema:
        """Build schema for confirm step."""
        fields: dict = {}
        # Always offer adapter KEY field
        fields[vol.Optional(CONF_ADAPTER_KEY)] = str
        # API key for BRP072C/BRP084
        if dev.get("adp_kind") == "4":
            fields[vol.Optional(CONF_API_KEY)] = str
        # Password for lpw-protected adapters
        if dev.get("lpw_flag") == "1":
            fields[vol.Optional(CONF_PASSWORD)] = str
        return vol.Schema(fields)

    def _confirm_placeholders(self, dev: dict, safety: dict) -> dict:
        """Build description placeholders for confirm step."""
        name = _decode_daikin_name(dev.get("name", "Daikin AC"))
        ver = dev.get("ver", "?").replace("_", ".")
        mac = dev.get("mac", "?")
        ssid = dev.get("ssid", "?")
        fw_status = "SAFE" if safety["safe"] else f"WARNING: {safety['warning']}"
        return {
            "name": name,
            "ip": dev.get("ip", self.host or "?"),
            "mac": mac,
            "firmware": ver,
            "ssid": ssid,
            "fw_status": fw_status,
        }

    async def _finalize(
        self,
        host: str,
        api_key: str | None = None,
        password: str | None = None,
        adapter_key: str | None = None,
    ) -> ConfigFlowResult:
        """Final step: connect via pydaikin, store entry."""
        if api_key:
            uuid = str(uuid4())
        else:
            uuid = None
            api_key = None
        if not password:
            password = None

        try:
            async with asyncio.timeout(TIMEOUT):
                device: Appliance = await DaikinFactory(
                    host,
                    async_get_clientsession(self.hass),
                    key=api_key,
                    uuid=uuid,
                    password=password,
                    ssl_context=get_daikin_ssl_context(),
                )
        except (TimeoutError, ClientError):
            return self.async_show_form(
                step_id="confirm",
                data_schema=self._confirm_schema(
                    self.selected_device or {}, {"safe": True}
                ),
                errors={"base": "cannot_connect"},
            )
        except web_exceptions.HTTPForbidden:
            return self.async_show_form(
                step_id="confirm",
                data_schema=self._confirm_schema(
                    self.selected_device or {}, {"safe": True}
                ),
                errors={"base": "invalid_auth"},
            )
        except Exception:
            _LOGGER.exception("Unexpected error connecting to %s", host)
            return self.async_show_form(
                step_id="confirm",
                data_schema=self._confirm_schema(
                    self.selected_device or {}, {"safe": True}
                ),
                errors={"base": "unknown"},
            )

        mac = device.mac

        # Auto-retrieve SPW for future re-pairing
        spw = None
        try:
            session = async_get_clientsession(self.hass)
            spw = await get_spw(session, host)
            if spw:
                _LOGGER.info("ACM: stored SPW for %s for re-pairing", mac)
        except Exception:
            pass

        # Firmware safety check + log
        try:
            session = async_get_clientsession(self.hass)
            info = await get_basic_info(session, host)
            fw_ver = info.get("ver", "").replace("_", ".")
            safety = check_firmware_safety(fw_ver)
            if not safety["safe"]:
                _LOGGER.warning("ACM FIRMWARE WARNING: %s", safety["warning"])
            else:
                _LOGGER.info("ACM: firmware %s is safe for local API", fw_ver)
        except Exception:
            fw_ver = ""

        # KEY contribution link
        if adapter_key and mac:
            url = generate_contribution_url(
                mac, adapter_key, firmware_ver=fw_ver
            )
            _LOGGER.info(
                "ACM: contribute your KEY to the community: %s", url
            )

        if not self.unique_id:
            await self.async_set_unique_id(mac)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=_decode_daikin_name(
                (self.selected_device or {}).get("name", host)
            ),
            data={
                CONF_HOST: host,
                KEY_MAC: mac,
                CONF_API_KEY: api_key,
                CONF_UUID: uuid,
                CONF_PASSWORD: password,
                CONF_SPW: spw,
                CONF_ADAPTER_KEY: adapter_key,
            },
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manual IP entry fallback."""
        if user_input is not None:
            self.host = user_input[CONF_HOST]
            # Fetch basic info to populate selected_device
            try:
                session = async_get_clientsession(self.hass)
                info = await get_basic_info(session, self.host)
                self.selected_device = info
                self.selected_device["ip"] = self.host
                mac = info.get("mac", "")
                if mac:
                    await self.async_set_unique_id(mac)
                    self._abort_if_unique_id_configured()
                return await self.async_step_confirm()
            except Exception:
                return self.async_show_form(
                    step_id="manual",
                    data_schema=vol.Schema({
                        vol.Required(CONF_HOST, default=self.host): str,
                    }),
                    errors={"base": "cannot_connect"},
                )

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({
                vol.Required(CONF_HOST, default=self.host): str,
            }),
        )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle zeroconf discovery."""
        _LOGGER.debug("Zeroconf: %s", discovery_info)
        try:
            session = async_get_clientsession(self.hass)
            info = await get_basic_info(session, discovery_info.host)
            mac = info.get("mac", "")
            if not mac:
                return self.async_abort(reason="cannot_connect")
            await self.async_set_unique_id(mac)
            self._abort_if_unique_id_configured()
            self.host = discovery_info.host
            self.selected_device = info
            self.selected_device["ip"] = discovery_info.host
            return await self.async_step_confirm()
        except Exception:
            return self.async_abort(reason="cannot_connect")
