# MAG Importer for Blender

Imports **characters, materials, animations, weapons, props, vehicles and complete maps** from
*MAG* (PS3, Zipper Interactive, 2010) directly out of the decrypted disc image. Nothing has to be
extracted by hand.

Tested with Blender 5.2.2 LTS and `MAG (USA).dec.iso` (retail v1.0, BCUS98110). It needs Blender
4.4 or newer.

## Install

```
python tools/build_extension.py        # -> dist/mag_importer-1.1.0.zip
```

In Blender: *Edit → Preferences → Get Extensions → (▾) Install from Disk…* and pick the zip.
You can also use *Add-ons → Install…* for a legacy install. Either way the panel appears in
**3D Viewport → Sidebar (N) → MAG**.

## Use

1. Click **Select ISO** and pick the decrypted disc image. The first scan indexes the whole
   15 GB disc in about 20 seconds. The scan runs in the background, shows a progress bar, and
   can be cancelled with Esc.
2. Choose a category, pick an item in the list (the list has a search filter), then click **Import**.

| Category | What you get |
| --- | --- |
| Characters | Skinned soldier and armor models, plus an armature fitted automatically (`root_main` body, `root_arms` first-person arms, `chute_root` parachute). Bodies come with a *Heads* collection: all 15 heads on the same rig, head 0 shown, the others hidden (toggle them in the Outliner) |
| Weapons | Third-person models (with stock) and the *(1st person)* view models, each with its attachments mounted on the sockets: the required sight is shown, the other compatible attachments (scopes, suppressor, foregrip, bipod, grenade launcher) wait hidden in an *Attachments* collection |
| Attachments, Gear, Vehicles, Props | The node hierarchy as objects, with meshes and materials. Vehicles keep their wheels and turrets at LOD 0 (the game only has LOD 1+ meshes for them), and their turret machine guns are mounted (M2HB, Kord, XM312, the deployable turrets' miniguns) unless the gun is part of the vehicle model |
| Maps | Every placed piece as a collection instance, the terrain, the scenery outside the terrain (mountains, treeline cards) in a separate backdrop collection, the sky as the scene World, the baked vertex lighting of trees and bushes, and optionally the collision meshes |
| Animations | A Blender action on the matching armature (the active one if it fits, otherwise a new one). Additive clips (`a_*` aim sweeps, recoils, flinches) play on top of the rig's current clip, which moves to the *MAG base* NLA track; import a normal clip first to choose the base. Characters can also bring their animations with them: see the **Animations** option |

### Options (Import Options sub-panel)

**Lower LODs**, **Lightmaps**, **Collision** and **Pack Textures** are add-on preferences (*Edit → Preferences →
Add-ons → MAG Importer*, also shown in the sub-panel), so they keep their value across files and
restarts. The other options are stored per scene.

* **LOD**: the level of detail to import, from 0 (highest) to 3.
* **Pack Textures** (off by default): packs the imported textures into the .blend so it works on
  another computer or after the texture cache is cleared, at the cost of a bigger file. **Pack
  MAG Textures** (bottom of the sub-panel) packs the textures of an earlier import.
* **Lower LODs** (off by default): models and characters also get every coarser LOD than the one
  above, in hidden `<name>_LOD1`, `_LOD2`... child collections (show one with its eye icon). LOD
  objects are parented to their node; character LODs are skinned to the same armature. Maps
  always use a single LOD.
* **Materials**: Principled BSDF materials with base colour, normal maps, specular/gloss and
  detail normal/colour maps, built from the material record's header: alpha test with the game's
  reference, blended glass and other translucent surfaces, water, and the material's UV
  transform. Normal maps can be DXT5nm or RGB; the add-on detects which.
* **Flip Normal Green**: inverts the Y channel of the normal maps.
* **Skeleton**: *Automatic* scores each skeleton's bind pose against the mesh, or you can force one.
* **Animations** (characters): *None* (default) imports the bind pose only. *Idle* adds the
  skeleton's idle clip as an action (`stand_p_idle` for third-person bodies, `fps_idle` for
  first-person arms). *All* adds every clip of the skeleton (461 for bodies, about 6 s; 208 for
  arms). **Filter** keeps only the clips whose name contains its text (for example `reload` or
  `stand_p_`). The idle clip (or the first clip) is assigned and sets the scene frame range. The
  others keep a fake user, so switch them in the Action Editor.
* **Set Scene to 30 fps**: MAG animations are authored at 30 fps.
* **Terrain**: imports the terrain tiles of one level of the terrain's LOD tree.
  * **LOD**: 0 (default) is the finest level. Each step up halves the tile count with coarser
    geometry, down to 2 tiles (clamped per map). Road decal meshes exist only at level 0; coarser
    tiles have the roads in their textures.
  * **Detail**: *Full Blend* (default) blends the four tiled ground layers, colour and normal, by
    each tile's weight map (shared *MAG Terrain* node group). *Macro Only* uses the tile's
    low-resolution blended colour, which is lighter and is what the game shows far away.
  * **Macro Fade** (default 250 m): with *Full Blend*, the ground fades from the layers to the
    macro colour between this camera distance and twice it, which removes the layers' repeat
    pattern from far away. 0 turns it off. It is shared by all terrain: change it later in the
    *Macro Fade Start* / *End* value nodes inside the *MAG Terrain* node group.
* **Collection Instances**: turn this off to get linked mesh duplicates instead of instances.
* **Lightmaps** (on by default): applies the lighting the game baked into the vertices of
  some placements (mostly trees and bushes, a few hundred to 3,500 per map). Each lit placement
  becomes plain objects with its own mesh copies (`mag_lighting` custom property) carrying a
  `mag_light` colour attribute: sun visibility in G (0 in shadow), a sky/ambient term in R. Their
  materials multiply the base colour by `0.75 × sky + 1.0 × sun` (the *Sky Weight* / *Sun Weight*
  value nodes in the shared *MAG Baked Light* node group).
* **Sky**: *World* (default) makes the map's sky the scene World (`MAG_<map>_sky`), which also
  lights EEVEE and Cycles renders. It replaces the scene's current World. *Dome* imports the
  game's sky dome mesh instead, scaled to 2.5 km around the map origin with an unlit material
  (in `MAG_<map>_sky`). *None* skips it. **Strength** sets the World's background strength. The
  World's *Sky Rotation* (a Mapping node) turns or mirrors the sky.
* **Collision** (off by default): imports the map's collision meshes into `MAG_<map>_collision`:
  `col_terrain` and a `col_<hash>` object per placed piece that has collision (linked duplicates
  of one mesh per shape). They are drawn as orange wireframe and hidden from renders; the
  collision includes the invisible boundary walls around the playable area.

## Limitations

* Foliage (grass, bushes, clutter) is off by default: turn on **Foliage** in the import options.
  It is placed with Geometry Nodes, one point cloud per foliage model. **Distance Fade** (off by
  default) applies the game's fade: plants shrink away between each model's fade start and end
  distance (often 50–60 m) from the **scene camera** (not the viewport view). Each foliage
  object's *MAG Foliage* modifier has the *Distance Fade*, *Fade Start*, *Fade End* and *Fade
  Scale* inputs; raise *Fade Scale* to draw further than the game. The per-instance colour is
  multiplied into the base colour at 50% (the *Foliage Tint* node in each `_foliage` material).
* Particle effects are not imported. Their emitter placements (and the 5 placements of one piece
  cut from the disc) are skipped and counted in the import report. Turn on **Emitters as
  Empties** to get a sphere empty (`fx_<hash>`, `missing_<hash>`) at each one, in `MAG_<map>_fx`.
* The terrain's macro fade distance is a guess (250–500 m): the game's switch distance has not
  been found. The layers' alpha masks and specular values are not used.
* Baked lighting: the weights that turn the sky and sun terms into a brightness are a calibration
  (the median sunlit vertex comes out at 1.0), not the game's formula. About 6 % of lit placements
  (16 models, such as Baku's most common tree) store extra entries between their meshes; the
  importer fits each mesh's offset from the lighting itself (see the console), which is reliable
  for models placed many times and a best guess for the two Kennicott models placed once.
* The sky's orientation is unconfirmed: the dome has no UVs, and the importer maps its top-down
  texture with a planar projection that may be mirrored or turned compared with the game. Fix
  it in the World's *Sky Rotation* node if a sun or landmark disagrees. The sky's fog and tint
  values are not used.
* Materials map the main texture slots to a Principled BSDF. Emissive, mask and environment
  slots are added as unconnected image nodes so you can wire them in. Most shader constants
  (specular colours and exponents, tints) have no known meaning yet and are not used.
* Water and glass are recognised by their shader. Water gets a glossy base in the material's
  water colour, glass a clear blended surface; neither reproduces the game's reflections or
  refraction. Workbench's *Texture* mode draws them from an unconnected 1×1 *Viewport colour*
  image.

## More

How the cache works, what makes imports fast, the code layout, a summary of the formats and the
developer tools are in [docs/internals.md](docs/internals.md). The full format notes are in
[docs/README.md](docs/README.md).
