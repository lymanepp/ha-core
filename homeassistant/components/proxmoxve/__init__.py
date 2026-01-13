"""Support for Proxmox VE."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from proxmoxer import AuthenticationError, ProxmoxAPI
import requests.exceptions
from requests.exceptions import ConnectTimeout, SSLError
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, issue_registry as ir
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .common import ProxmoxClient, call_api_container_vm, parse_api_container_vm
from .const import (
    CONF_CONTAINERS,
    CONF_NODE,
    CONF_NODES,
    CONF_REALM,
    CONF_VMS,
    DEFAULT_PORT,
    DEFAULT_REALM,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    TYPE_CONTAINER,
    TYPE_VM,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__package__)

type ProxmoxVEConfigEntry = ConfigEntry[ProxmoxVERuntimeData]

PLATFORMS = [Platform.BINARY_SENSOR]

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.All(
            cv.ensure_list,
            [
                vol.Schema(
                    {
                        vol.Required(CONF_HOST): cv.string,
                        vol.Required(CONF_USERNAME): cv.string,
                        vol.Required(CONF_PASSWORD): cv.string,
                        vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
                        vol.Optional(CONF_REALM, default=DEFAULT_REALM): cv.string,
                        vol.Optional(
                            CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL
                        ): cv.boolean,
                        vol.Required(CONF_NODES): vol.All(
                            cv.ensure_list,
                            [
                                vol.Schema(
                                    {
                                        vol.Required(CONF_NODE): cv.string,
                                        vol.Optional(CONF_VMS, default=[]): [
                                            cv.positive_int
                                        ],
                                        vol.Optional(CONF_CONTAINERS, default=[]): [
                                            cv.positive_int
                                        ],
                                    }
                                )
                            ],
                        ),
                    }
                )
            ],
        )
    },
    extra=vol.ALLOW_EXTRA,
)


class ProxmoxVERuntimeData:
    """Runtime data for Proxmox VE."""

    def __init__(
        self,
        proxmox_client: ProxmoxClient,
        coordinators: dict[
            str, dict[int, DataUpdateCoordinator[dict[str, Any] | None]]
        ],
    ) -> None:
        """Initialize runtime data."""
        self.proxmox_client = proxmox_client
        self.coordinators = coordinators


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Proxmox VE component."""
    hass.data.setdefault(DOMAIN, {})

    # Import YAML configurations
    if DOMAIN in config:
        for entry_config in config[DOMAIN]:
            hass.async_create_task(
                hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={"source": "import"},
                    data=entry_config,
                )
            )

        # Create repair issue to inform user about YAML deprecation
        ir.async_create_issue(
            hass,
            DOMAIN,
            "yaml_deprecated",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="yaml_deprecated",
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ProxmoxVEConfigEntry) -> bool:
    """Set up Proxmox VE from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Build Proxmox client
    def build_client() -> ProxmoxClient:
        """Build the Proxmox client connection."""
        proxmox_client = ProxmoxClient(
            entry.data[CONF_HOST],
            entry.data[CONF_PORT],
            entry.data[CONF_USERNAME],
            entry.data[CONF_REALM],
            entry.data[CONF_PASSWORD],
            entry.data[CONF_VERIFY_SSL],
        )
        proxmox_client.build_client()
        return proxmox_client

    try:
        proxmox_client = await hass.async_add_executor_job(build_client)
    except AuthenticationError as err:
        raise ConfigEntryAuthFailed("Invalid credentials") from err
    except SSLError as err:
        raise ConfigEntryNotReady(f"Unable to verify SSL certificate: {err}") from err
    except ConnectTimeout as err:
        raise ConfigEntryNotReady(f"Connection timeout: {err}") from err
    except requests.exceptions.ConnectionError as err:
        raise ConfigEntryNotReady(f"Cannot connect to host: {err}") from err

    proxmox = proxmox_client.get_api_client()

    # Create coordinators for each VM/container from options
    coordinators: dict[
        str, dict[int, DataUpdateCoordinator[dict[str, Any] | None]]
    ] = {}

    for node_config in entry.options.get(CONF_NODES, []):
        node_name = node_config[CONF_NODE]
        coordinators[node_name] = {}

        for vm_id in node_config.get(CONF_VMS, []):
            coordinator = create_coordinator_container_vm(
                hass, proxmox, node_name, vm_id, TYPE_VM, entry
            )

            # Fetch initial data
            await coordinator.async_refresh()

            coordinators[node_name][vm_id] = coordinator

        for container_id in node_config.get(CONF_CONTAINERS, []):
            coordinator = create_coordinator_container_vm(
                hass, proxmox, node_name, container_id, TYPE_CONTAINER, entry
            )

            # Fetch initial data
            await coordinator.async_refresh()

            coordinators[node_name][container_id] = coordinator

    # Store runtime data
    entry.runtime_data = ProxmoxVERuntimeData(proxmox_client, coordinators)

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register update listener for options changes
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ProxmoxVEConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(hass: HomeAssistant, entry: ProxmoxVEConfigEntry) -> None:
    """Reload config entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


def create_coordinator_container_vm(
    hass: HomeAssistant,
    proxmox: ProxmoxAPI,
    node_name: str,
    vm_id: int,
    vm_type: int,
    config_entry: ConfigEntry | None = None,
) -> DataUpdateCoordinator[dict[str, Any] | None]:
    """Create and return a DataUpdateCoordinator for a vm/container."""

    async def async_update_data() -> dict[str, Any] | None:
        """Call the api and handle the response."""

        def poll_api() -> dict[str, Any] | None:
            """Call the api."""
            return call_api_container_vm(proxmox, node_name, vm_id, vm_type)

        try:
            vm_status = await hass.async_add_executor_job(poll_api)
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed("Authentication failed") from err
        except requests.exceptions.ConnectionError as err:
            raise UpdateFailed(f"Connection error: {err}") from err

        if vm_status is None:
            _LOGGER.warning(
                "VM/Container %s unable to be found in node %s", vm_id, node_name
            )
            return None

        return parse_api_container_vm(vm_status)

    return DataUpdateCoordinator(
        hass,
        _LOGGER,
        config_entry=config_entry,
        name=f"proxmox_coordinator_{node_name}_{vm_id}",
        update_method=async_update_data,
        update_interval=timedelta(seconds=UPDATE_INTERVAL),
    )
