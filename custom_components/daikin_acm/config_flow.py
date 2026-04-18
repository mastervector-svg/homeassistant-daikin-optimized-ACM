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
            return await self.async_step_no_devices()

        # Filter out already-configured adapters
        configured_macs = set()
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            mac = entry.data.get(KEY_MAC, "")
            if mac:
                configured_macs.add(mac.upper().replace(":", ""))

        new_devices = [
            dev for dev in self.discovered
            if dev.get("mac", "").upper() not in configured_macs
        ]

        if not new_devices:
            return self.async_abort(reason="all_configured")

        # Build selection list
        device_options = {}
        for dev in new_devices:
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
        """Step 3: Auto-setup for adapters already on network.

        For adapters on the network:
        - Auto-fetch SPW via /common/get_spw (no auth needed)
        - Auto-register if needed
        - Skip straight to finalize — no form needed

        Only shows form if adapter requires manual KEY (AP mode / BRP072C).
        """
        dev = self.selected_device or {}
        name = _decode_daikin_name(dev.get("name", "Daikin AC"))
        ver = dev.get("ver", "?").replace("_", ".")
        mac = dev.get("mac", "")
        adp_kind = dev.get("adp_kind", "")
        lpw_flag = dev.get("lpw_flag", "0")
        safety = check_firmware_safety(ver)

        session = async_get_clientsession(self.hass)

        # For adapters already on network (not AP mode): auto-setup
        if self.host and not self.host.startswith("192.168.127.") and not self.host.startswith("192.168.0.100"):
            # Auto-fetch SPW
            spw = None
            try:
                spw = await get_spw(session, self.host)
                if spw:
                    _LOGGER.info("ACM: auto-retrieved SPW for %s", mac)
            except Exception:
                pass

            # BRP072C/BRP084 (adp_kind=4) needs API key — must ask
            if adp_kind == "4":
                if user_input is not None:
                    new_name = user_input.get("device_name")
                    if new_name:
                        await self._set_adapter_name(self.host, new_name)
                    return await self._finalize(
                        host=self.host,
                        api_key=user_input.get(CONF_API_KEY),
                        password=user_input.get(CONF_PASSWORD),
                        spw=spw,
                        custom_name=new_name,
                    )
                return self.async_show_form(
                    step_id="confirm",
                    data_schema=vol.Schema({
                        vol.Optional("device_name", default=name): str,
                        vol.Required(CONF_API_KEY): str,
                    }),
                    description_placeholders={
                        "name": name, "ip": self.host, "mac": mac,
                        "firmware": ver, "ssid": dev.get("ssid", "?"),
                        "fw_status": "SAFE" if safety["safe"] else safety["warning"],
                    },
                )

            # BRP069 (adp_kind=3) — ask for name only, rest is automatic
            if user_input is not None:
                new_name = user_input.get("device_name")
                if new_name:
                    await self._set_adapter_name(self.host, new_name)
                return await self._finalize(
                    host=self.host, spw=spw, custom_name=new_name,
                )

            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({
                    vol.Optional("device_name", default=name): str,
                }),
                description_placeholders={
                    "name": name, "ip": self.host, "mac": mac,
                    "firmware": ver, "ssid": dev.get("ssid", "?"),
                    "fw_status": "SAFE" if safety["safe"] else safety["warning"],
                },
            )

        # AP mode (192.168.127.1) — need KEY from sticker
        if user_input is not None:
            adapter_key = user_input.get(CONF_ADAPTER_KEY, "")
            if adapter_key:
                ok = await register_terminal(session, self.host, adapter_key)
                if not ok:
                    return self.async_show_form(
                        step_id="confirm",
                        data_schema=vol.Schema({
                            vol.Required(CONF_ADAPTER_KEY): str,
                        }),
                        errors={"base": "invalid_auth"},
                        description_placeholders={
                            "name": name, "ip": self.host, "mac": mac,
                            "firmware": ver, "ssid": dev.get("ssid", "?"),
                            "fw_status": "AP MODE — enter KEY from sticker",
                        },
                    )
                _LOGGER.info("ACM: registered terminal on %s", self.host)
            return await self._finalize(
                host=self.host, adapter_key=adapter_key or None,
            )

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({
                vol.Required(CONF_ADAPTER_KEY): str,
            }),
            description_placeholders={
                "name": name, "ip": self.host, "mac": mac,
                "firmware": ver, "ssid": dev.get("ssid", "?"),
                "fw_status": "AP MODE — enter KEY from sticker",
            },
        )

    async def _set_adapter_name(self, host: str, name: str) -> None:
        """Write name to adapter via /common/set_name."""
        from urllib.parse import quote
        session = async_get_clientsession(self.hass)
        encoded = quote(name)
        url = f"http://{host}/common/set_name?name={encoded}"
        try:
            async with session.get(url, timeout=asyncio.timeout(10)) as resp:
                text = await resp.text()
                if "OK" in text:
                    _LOGGER.info("ACM: set adapter name to '%s' on %s", name, host)
                else:
                    _LOGGER.warning("ACM: set_name returned: %s", text)
        except Exception as err:
            _LOGGER.debug("ACM: set_name failed for %s: %s", host, err)

    async def _finalize(
        self,
        host: str,
        api_key: str | None = None,
        password: str | None = None,
        adapter_key: str | None = None,
        spw: str | None = None,
        custom_name: str | None = None,
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

        # Auto-retrieve SPW for future re-pairing (if not already provided)
        if not spw:
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

        entry_title = custom_name or _decode_daikin_name(
            (self.selected_device or {}).get("name", host)
        )
        return self.async_create_entry(
            title=entry_title,
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

    async def async_step_no_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """No adapters found on network. Offer AP mode setup or manual IP."""
        if user_input is not None:
            action = user_input.get("action", "manual")
            if action == "ap_setup":
                return await self.async_step_ap_setup()
            return await self.async_step_manual()

        return self.async_show_form(
            step_id="no_devices",
            data_schema=vol.Schema({
                vol.Required("action", default="ap_setup"): vol.In({
                    "ap_setup": "I have an adapter in AP mode (blinking LED)",
                    "manual": "I know the IP address",
                }),
            }),
        )

    async def async_step_ap_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """AP mode setup: connect to adapter, enter KEY, scan WiFi, send credentials."""
        if user_input is not None:
            adapter_key = user_input.get(CONF_ADAPTER_KEY, "")
            wifi_ssid = user_input.get("wifi_ssid", "")
            wifi_password = user_input.get("wifi_password", "")
            ap_host = user_input.get("ap_host", "192.168.127.1")

            if not adapter_key:
                return self.async_show_form(
                    step_id="ap_setup",
                    data_schema=self._ap_schema(),
                    errors={"base": "invalid_auth"},
                )

            session = async_get_clientsession(self.hass)

            # Register with KEY
            ok = await register_terminal(session, ap_host, adapter_key)
            if not ok:
                return self.async_show_form(
                    step_id="ap_setup",
                    data_schema=self._ap_schema(),
                    errors={"base": "invalid_auth"},
                )
            _LOGGER.info("ACM AP: registered with adapter at %s", ap_host)

            # Send WiFi credentials
            if wifi_ssid and wifi_password:
                from .provisioning import connect_wifi, reboot_adapter
                ok = await connect_wifi(session, ap_host, wifi_ssid, wifi_password)
                if ok:
                    _LOGGER.info("ACM AP: WiFi credentials sent, rebooting adapter")
                    await reboot_adapter(session, ap_host)
                    return self.async_abort(
                        reason="ap_setup_complete",
                    )
                else:
                    return self.async_show_form(
                        step_id="ap_setup",
                        data_schema=self._ap_schema(),
                        errors={"base": "cannot_connect"},
                    )

            return self.async_abort(reason="ap_setup_complete")

        return self.async_show_form(
            step_id="ap_setup",
            data_schema=self._ap_schema(),
        )

    def _ap_schema(self) -> vol.Schema:
        return vol.Schema({
            vol.Required("ap_host", default="192.168.127.1"): str,
            vol.Required(CONF_ADAPTER_KEY): str,
            vol.Required("wifi_ssid"): str,
            vol.Required("wifi_password"): str,
        })

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
