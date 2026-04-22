# Zone Mesh Animation Pipeline

This repo now includes a deterministic renderer for the state-level Hyperon/K1S cognitive-mesh animation set.

Source files:

- `tools/zone_mesh_anim.py`
- `tools/zone_mesh_scene.json`
- `tools/zone_mesh_profiles.json`

Default outputs land in `state/zone_mesh_outputs/`:

- `zone_mesh_density_low.mp4`
- `zone_mesh_density_low.webm`
- `zone_mesh_density_low.gif`
- `zone_mesh_density_medium.mp4`
- `zone_mesh_density_medium.webm`
- `zone_mesh_density_medium.gif`
- `zone_mesh_density_high.mp4`
- `zone_mesh_density_high.webm`
- `zone_mesh_density_high.gif`

## Usage

Inspect the generated topology for one density:

```bash
python tools/zone_mesh_anim.py inspect --profile low
```

Render the full set:

```bash
python tools/zone_mesh_anim.py render --profile all --format all
```

Render only GIFs:

```bash
python tools/zone_mesh_anim.py render --profile all --format gif
```

Render a short debug sample:

```bash
python tools/zone_mesh_anim.py render --profile medium --format gif --frame-limit 24
```

## Visual Semantics

- cyan rings and cyan paths represent live query motion
- amber packets and amber paths represent chunk transfer and cache warming
- magenta pulses represent inference-call orchestration
- gold halos represent warm neighborhood retention

## Story Beats

All three density variants use the same four-beat narrative:

1. topology reveal
2. local-first query
3. bounded remote expansion with summary pull
4. inference on the warmed regional mesh

The only intended variable across `low`, `medium`, and `high` is mesh density and motion volume. This keeps the set directly comparable.
