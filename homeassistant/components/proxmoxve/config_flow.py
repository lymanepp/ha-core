"""Config flow for Proxmox VE integration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from proxmoxer import AuthenticationError, ProxmoxAPI
import requests.exceptions
from requests.exceptions import ConnectTimeout, SSLError
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .common import ProxmoxClient
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
)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Optional(CONF_REALM, default=DEFAULT_REALM): str,
        vol.Optional(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): bool,
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from STEP_USER_DATA_SCHEMA with values provided by the user.
    """

    def build_client() -> tuple[ProxmoxClient, list[str]]:
        """Build the Proxmox client connection and get nodes."""
        proxmox_client = ProxmoxClient(
            data[CONF_HOST],
            data[CONF_PORT],
            data[CONF_USERNAME],
            data[CONF_REALM],
            data[CONF_PASSWORD],
            data[CONF_VERIFY_SSL],
        )
        proxmox_client.build_client()
        
        # Get available nodes
        proxmox = proxmox_client.get_api_client()
        nodes = [node["node"] for node in proxmox.nodes.get()]
        
        return proxmox_client, nodes

    try:
        client, nodes = await hass.async_add_executor_job(build_client)
    except AuthenticationError as err:
        raise InvalidAuth from err
    except SSLError as err:
        raise CannotConnect("SSL verification failed") from err
    except ConnectTimeout as err:
        raise CannotConnect("Connection timeout") from err
    except requests.exceptions.ConnectionError as err:
        raise CannotConnect("Cannot connect to host") from err
    except Exception as err:
        raise CannotConnect(f"Unexpected error: {err}") from err

    # Return info that you want to store in the config entry.
    return {"title": data[CONF_HOST], "nodes": nodes}


class ProxmoxVEConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Proxmox VE."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovered_nodes: list[str] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except CannotConnect as err:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:
                errors["base"] = "unknown"
            else:
                # Set unique ID to prevent duplicate entries for same host
                await self.async_set_unique_id(user_input[CONF_HOST])
                self._abort_if_unique_id_configured()

                # Store discovered nodes for options flow
                self._discovered_nodes = info["nodes"]

                # Create entry without node selection (will be done in options flow)
                return self.async_create_entry(
                    title=info["title"],
                    data=user_input,
                    options={CONF_NODES: []},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauth flow."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reauth confirm."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()

        if user_input is not None:
            data = {
                **reauth_entry.data,
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            }

            try:
                await validate_input(self.hass, data)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
            description_placeholders={
                "host": reauth_entry.data[CONF_HOST],
                "username": reauth_entry.data[CONF_USERNAME],
            },
        )

    async def async_step_import(self, import_config: dict[str, Any]) -> ConfigFlowResult:
        """Handle import from YAML configuration."""
        # Extract host-level config
        host_data = {
            CONF_HOST: import_config[CONF_HOST],
            CONF_USERNAME: import_config[CONF_USERNAME],
            CONF_PASSWORD: import_config[CONF_PASSWORD],
            CONF_PORT: import_config.get(CONF_PORT, DEFAULT_PORT),
            CONF_REALM: import_config.get(CONF_REALM, DEFAULT_REALM),
            CONF_VERIFY_SSL: import_config.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
        }

        # Check if already configured
        await self.async_set_unique_id(host_data[CONF_HOST])
        self._abort_if_unique_id_configured()

        # Validate connection
        try:
            info = await validate_input(self.hass, host_data)
        except (CannotConnect, InvalidAuth):
            return self.async_abort(reason="cannot_connect")

        # Convert YAML nodes structure to options format
        nodes_config = []
        for node in import_config.get(CONF_NODES, []):
            node_data = {
                CONF_NODE: node[CONF_NODE],
                CONF_VMS: node.get(CONF_VMS, []),
                CONF_CONTAINERS: node.get(CONF_CONTAINERS, []),
            }
            nodes_config.append(node_data)

        return self.async_create_entry(
            title=info["title"],
            data=host_data,
            options={CONF_NODES: nodes_config},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return ProxmoxVEOptionsFlow(config_entry)


class ProxmoxVEOptionsFlow(OptionsFlow):
    """Handle options flow for Proxmox VE."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry
        self._nodes: list[str] = []
        self._current_node: str | None = None
        self._nodes_config: dict[str, dict[str, list[int]]] = {}
        self._available_vms: dict[int, str] = {}
        self._available_containers: dict[int, str] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        # Build client to discover available nodes and VMs/containers
        def get_proxmox_data() -> tuple[list[str], dict[str, dict[str, dict[int, str]]]]:
            """Get nodes, VMs, and containers from Proxmox."""
            proxmox_client = ProxmoxClient(
                self.config_entry.data[CONF_HOST],
                self.config_entry.data[CONF_PORT],
                self.config_entry.data[CONF_USERNAME],
                self.config_entry.data[CONF_REALM],
                self.config_entry.data[CONF_PASSWORD],
                self.config_entry.data[CONF_VERIFY_SSL],
            )
            proxmox_client.build_client()
            proxmox = proxmox_client.get_api_client()

            nodes = [node["node"] for node in proxmox.nodes.get()]
            
            # Get VMs and containers for all nodes
            node_resources: dict[str, dict[str, dict[int, str]]] = {}
            for node in nodes:
                vms = {}
                containers = {}
                
                # Get VMs
                try:
                    for vm in proxmox.nodes(node).qemu.get():
                        vms[vm["vmid"]] = vm.get("name", f"VM {vm['vmid']}")
                except Exception:
                    pass
                
                # Get containers
                try:
                    for container in proxmox.nodes(node).lxc.get():
                        containers[container["vmid"]] = container.get("name", f"CT {container['vmid']}")
                except Exception:
                    pass
                
                node_resources[node] = {"vms": vms, "containers": containers}
            
            return nodes, node_resources

        try:
            self._nodes, node_resources = await self.hass.async_add_executor_job(
                get_proxmox_data
            )
        except Exception:
            return self.async_abort(reason="cannot_connect")

        # Load existing configuration
        existing_config = self.config_entry.options.get(CONF_NODES, [])
        self._nodes_config = {}
        for node_cfg in existing_config:
            node_name = node_cfg[CONF_NODE]
            self._nodes_config[node_name] = {
                CONF_VMS: node_cfg.get(CONF_VMS, []),
                CONF_CONTAINERS: node_cfg.get(CONF_CONTAINERS, []),
            }

        # Store node resources for later steps
        self._node_resources = node_resources

        if user_input is not None:
            # Save the configuration
            nodes_list = []
            for node_name, config in self._nodes_config.items():
                if config[CONF_VMS] or config[CONF_CONTAINERS]:
                    nodes_list.append({
                        CONF_NODE: node_name,
                        CONF_VMS: config[CONF_VMS],
                        CONF_CONTAINERS: config[CONF_CONTAINERS],
                    })
            
            return self.async_create_entry(
                title="",
                data={CONF_NODES: nodes_list},
            )

        # Show node selection
        return await self.async_step_select_node()

    async def async_step_select_node(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select a node to configure."""
        if user_input is not None:
            # Check if user wants to finish
            if user_input.get("node") == "DONE":
                # User is done configuring nodes
                nodes_list = []
                for node_name, config in self._nodes_config.items():
                    if config[CONF_VMS] or config[CONF_CONTAINERS]:
                        nodes_list.append({
                            CONF_NODE: node_name,
                            CONF_VMS: config[CONF_VMS],
                            CONF_CONTAINERS: config[CONF_CONTAINERS],
                        })
                
                return self.async_create_entry(
                    title="",
                    data={CONF_NODES: nodes_list},
                )
            
            # User selected a node to configure
            self._current_node = user_input["node"]
            return await self.async_step_select_resources()

        # Build list of nodes with their current status
        node_options = []
        for node in self._nodes:
            vms_count = len(self._nodes_config.get(node, {}).get(CONF_VMS, []))
            containers_count = len(self._nodes_config.get(node, {}).get(CONF_CONTAINERS, []))
            label = f"{node} ({vms_count} VMs, {containers_count} containers)"
            node_options.append({"value": node, "label": label})
        
        # Add "Done" option
        node_options.append({"value": "DONE", "label": "✓ Finish configuration"})

        return self.async_show_form(
            step_id="select_node",
            data_schema=vol.Schema({
                vol.Required("node"): SelectSelector(
                    SelectSelectorConfig(
                        options=node_options,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
            }),
            description_placeholders={
                "configured_count": str(len([n for n, c in self._nodes_config.items() if c[CONF_VMS] or c[CONF_CONTAINERS]])),
            },
            last_step=False,
        )

    async def async_step_select_resources(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select VMs and containers for the current node."""
        if user_input is not None:
            # Save the configuration for this node (convert strings to ints)
            self._nodes_config[self._current_node] = {
                CONF_VMS: [int(vm_id) for vm_id in user_input.get(CONF_VMS, [])],
                CONF_CONTAINERS: [
                    int(ct_id) for ct_id in user_input.get(CONF_CONTAINERS, [])
                ],
            }
            
            # Go back to node selection
            return await self.async_step_select_node()

        # Get available VMs and containers for this node
        node_data = self._node_resources.get(self._current_node, {})
        available_vms = node_data.get("vms", {})
        available_containers = node_data.get("containers", {})

        # Get current selection
        current_config = self._nodes_config.get(self._current_node, {})
        current_vms = current_config.get(CONF_VMS, [])
        current_containers = current_config.get(CONF_CONTAINERS, [])

        # Build VM options
        vm_options = [
            {"value": str(vmid), "label": name}
            for vmid, name in available_vms.items()
        ]
        
        # Build container options
        container_options = [
            {"value": str(ctid), "label": name}
            for ctid, name in available_containers.items()
        ]

        schema = {}
        
        if vm_options:
            schema[vol.Optional(CONF_VMS, default=current_vms)] = SelectSelector(
                SelectSelectorConfig(
                    options=vm_options,
                    multiple=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        
        if container_options:
            schema[vol.Optional(CONF_CONTAINERS, default=current_containers)] = SelectSelector(
                SelectSelectorConfig(
                    options=container_options,
                    multiple=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )

        if not schema:
            # No VMs or containers available on this node
            return self.async_abort(reason="no_resources")

        return self.async_show_form(
            step_id="select_resources",
            data_schema=vol.Schema(schema),
            description_placeholders={
                "node": self._current_node,
                "vm_count": str(len(available_vms)),
                "container_count": str(len(available_containers)),
            },
            last_step=False,
        )


class CannotConnect(Exception):
    """Error to indicate we cannot connect."""


class InvalidAuth(Exception):
    """Error to indicate there is invalid auth."""
