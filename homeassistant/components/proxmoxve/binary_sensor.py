"""Binary sensor to read Proxmox VE data."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import ProxmoxVEConfigEntry
from .const import CONF_CONTAINERS, CONF_NODE, CONF_NODES, CONF_VMS
from .entity import ProxmoxEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ProxmoxVEConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors from a config entry."""
    runtime_data = entry.runtime_data
    coordinators = runtime_data.coordinators

    sensors = []

    for node_config in entry.options.get(CONF_NODES, []):
        node_name = node_config[CONF_NODE]

        if node_name not in coordinators:
            continue

        for vm_id in node_config.get(CONF_VMS, []):
            if vm_id not in coordinators[node_name]:
                continue

            coordinator = coordinators[node_name][vm_id]

            # Skip if coordinator doesn't have data yet
            if (coordinator_data := coordinator.data) is None:
                continue

            name = coordinator_data["name"]
            sensor = create_binary_sensor(
                coordinator, entry.data["host"], node_name, vm_id, name
            )
            sensors.append(sensor)

        for container_id in node_config.get(CONF_CONTAINERS, []):
            if container_id not in coordinators[node_name]:
                continue

            coordinator = coordinators[node_name][container_id]

            # Skip if coordinator doesn't have data yet
            if (coordinator_data := coordinator.data) is None:
                continue

            name = coordinator_data["name"]
            sensor = create_binary_sensor(
                coordinator, entry.data["host"], node_name, container_id, name
            )
            sensors.append(sensor)

    async_add_entities(sensors)


def create_binary_sensor(
    coordinator,
    host_name: str,
    node_name: str,
    vm_id: int,
    name: str,
) -> ProxmoxBinarySensor:
    """Create a binary sensor based on the given data."""
    return ProxmoxBinarySensor(
        coordinator=coordinator,
        unique_id=f"proxmox_{node_name}_{vm_id}_running",
        name=f"{node_name}_{name}",
        icon="",
        host_name=host_name,
        node_name=node_name,
        vm_id=vm_id,
    )


class ProxmoxBinarySensor(ProxmoxEntity, BinarySensorEntity):
    """A binary sensor for reading Proxmox VE data."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        unique_id: str,
        name: str,
        icon: str,
        host_name: str,
        node_name: str,
        vm_id: int,
    ) -> None:
        """Create the binary sensor for vms or containers."""
        super().__init__(
            coordinator, unique_id, name, icon, host_name, node_name, vm_id
        )

    @property
    def is_on(self) -> bool | None:
        """Return the state of the binary sensor."""
        if (data := self.coordinator.data) is None:
            return None

        return data["status"] == "running"

    @property
    def available(self) -> bool:
        """Return sensor availability."""

        return super().available and self.coordinator.data is not None
