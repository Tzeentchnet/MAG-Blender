"""MAG (PS3, Zipper Interactive) importer for Blender.

Reads the game straight from the decrypted disc image: characters, materials, animations,
weapons, props, vehicles and complete maps.
"""

bl_info = {
    'name': 'MAG Importer',
    'author': 'MAG-Blender',
    'version': (1, 1, 0),
    'blender': (4, 4, 0),
    'location': '3D Viewport > Sidebar > MAG',
    'description': 'Import characters, weapons, vehicles, props, maps and animations from the MAG (PS3) ISO',
    'category': 'Import-Export',
}


def register():
    from . import blender
    blender.register()


def unregister():
    from . import blender
    blender.unregister()
