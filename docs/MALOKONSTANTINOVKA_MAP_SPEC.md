# Malokonstantinovka map integration (OWW)

This spec wires a custom strategic map layout based on the RP map references provided by the user.

## Included in repo
- `map/definition.csv` — starter province-color mapping for OWW tags.
- `map/adjacency_rules.txt` — initial sea/land strait-style adjacency rules.
- `map/default.map` — map engine entry file for province/state setup.
- `tools/build_map_assets.py` — utility that converts `sources/malokonstantinovka_reference.png` into required BMP assets and emits placeholders (`provinces.bmp`, `terrain.bmp`, `heightmap.bmp`, `rivers.bmp`).

## Workflow
1. Put the preferred final RP map image into `sources/malokonstantinovka_reference.png`.
2. Run:
   ```bash
   python tools/build_map_assets.py
   ```
3. Verify files generated in `map/`.
4. Continue by splitting each major country color into additional province IDs as needed for gameplay depth.

## Notes
- This is an MVP map pipeline designed to make the mod structurally map-ready.
- For full HOI4 production quality, province borders should be cleaned to single-color regions and validated with the Clausewitz map tools.
