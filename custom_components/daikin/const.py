"""Constants for Daikin ACM."""

DOMAIN = "daikin"

# KEY crowdsourcing — community GitHub issues, no external server
CONF_CONTRIBUTE_KEY = "contribute_key"

# Firmware safety
FIRMWARE_SAFE_VERSIONS = {
    "BRP069B": "1.14.88",  # last confirmed safe for /aircon/ API
    "BRP084C": "1.19.0",   # last before dsiot-only change
}
FIRMWARE_DANGEROUS_PREFIX = ["3.", "2.8"]

# WiFi provisioning
DAIKIN_AP_IPS = ["192.168.127.1", "192.168.0.100"]
DAIKIN_UDP_PORT = 30050
DAIKIN_UDP_MSG = "DAIKIN_UDP/common/basic_info"

# Stored credentials
CONF_SPW = "spw"
CONF_ADAPTER_KEY = "adapter_key"

ATTR_TARGET_TEMPERATURE = "target_temperature"
ATTR_INSIDE_TEMPERATURE = "inside_temperature"
ATTR_OUTSIDE_TEMPERATURE = "outside_temperature"

ATTR_TARGET_HUMIDITY = "target_humidity"
ATTR_HUMIDITY = "humidity"

ATTR_COMPRESSOR_FREQUENCY = "compressor_frequency"

ATTR_ENERGY_TODAY = "energy_today"
ATTR_COOL_ENERGY = "cool_energy"
ATTR_HEAT_ENERGY = "heat_energy"

ATTR_TOTAL_POWER = "total_power"
ATTR_TOTAL_ENERGY_TODAY = "total_energy_today"

ATTR_STATE_ON = "on"
ATTR_STATE_OFF = "off"

KEY_MAC = "mac"
KEY_IP = "ip"

TIMEOUT = 60

# Default polling interval for state updates (seconds)
# Reduced to 10s for better responsiveness to manual remote changes
# Can be overridden via options flow in the future
DEFAULT_UPDATE_INTERVAL = 10
