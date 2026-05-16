from dataclasses import dataclass


@dataclass
class Mod:
    name: str
    url: str
    enabled: bool = True
    status: str = "Ready"
    package_id: str = ""
    dependency_guid: str = ""


INITIAL_MODS = [
    Mod("Craft From Containers", "https://thunderstore.io/c/valheim/p/rendl0449/CraftFromContainers/").__dict__,
    Mod("Equipment and Quick Slots", "https://thunderstore.io/c/valheim/p/RandyKnapp/EquipmentAndQuickSlots/").__dict__,
    Mod(
        "Quick Stack Store Sort Trash",
        "https://thunderstore.io/c/valheim/p/Goldenrevolver/Quick_Stack_Store_Sort_Trash_Restock/",
    ).__dict__,
    Mod("MassFarming", "https://thunderstore.io/c/valheim/p/MainStreetGaming/MassFarming/").__dict__,
    Mod("Farmgrid", "https://thunderstore.io/c/valheim/p/Galateam/FarmGrid/").__dict__,
    Mod("Spyglass", "https://thunderstore.io/c/valheim/p/Advize/Spyglass/").__dict__,
    Mod("SmarterContainers", "https://thunderstore.io/c/valheim/p/Roses/SmarterContainers/").__dict__,
    Mod("AutoRepair", "https://thunderstore.io/c/valheim/p/Tekla/AutoRepair/").__dict__,
    Mod("Jotunn", "https://thunderstore.io/c/valheim/p/ValheimModding/Jotunn/").__dict__,
]
