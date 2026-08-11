# Ordering commercial imagery

Sentinel-2 tops out at 10 m. At low altitude that is magnified ~200x and turns
to mush. Buying 30–50 cm archive imagery is the only way past it.

## Before you spend anything: check the licence

This matters more than the price. Standard Maxar and Airbus licences are
**single-organisation, internal-use** by default. They generally do **not** allow
you to redistribute the imagery or derived products publicly.

`arctic-sim` bakes imagery into `sim/models/terrain_<site>/materials/textures/`.
If this repo — or anything built from it, such as a public Damn Vulnerable Drone
world — is published, that texture goes with it. That is redistribution.

Tell the reseller exactly what you intend:

> The imagery will be resampled into a texture used inside a 3D simulator. The
> simulator [is / is not] publicly distributed. I need a licence covering
> [internal use only / public redistribution of derived visual products].

A redistribution or "web-enabled derivative" licence costs more. Ask up front;
retrofitting it after purchase is harder and more expensive.

## What to send

Attach the AOI and quote the catalogue IDs. Both are in `docs/aoi/`:

| site | area | AOI files |
|---|---|---|
| fort_ross | 42.25 km² | `docs/aoi/fort_ross.geojson`, `.wkt` |
| pond_inlet | 25.00 km² | `docs/aoi/pond_inlet.geojson`, `.wkt` |
| resolute | 6.25 km² | `docs/aoi/resolute.geojson`, `.wkt` |

### Enquiry template

> I would like to price archive imagery over three small arctic AOIs
> (GeoJSON attached, WGS84).
>
> - fort_ross — 42.25 km² — Bellot Strait, Somerset Island, NU
> - pond_inlet — 25.00 km² — Mittimatalik, Baffin Island, NU
> - resolute — 6.25 km² — Resolute Bay, Cornwallis Island, NU
>
> Requirements: pan-sharpened natural colour, ≤50 cm, snow-free season
> (June–August), <10% cloud, orthorectified, GeoTIFF.
>
> Candidate Maxar catalogue IDs identified from stereo archive records:
>
>   fort_ross   WV02_20220720  10300100D6461900 / 10300100D65CD100  (0.41% cloud)
>   pond_inlet  WV03_20230714  104001008765F600 / 10400100882E3A00  (1.51% cloud, 31 cm)
>   resolute    WV02_20210705  10300100C1847C00 / 10300100C18F7800  (0.00% cloud)
>
> Please confirm availability, price per AOI, and licence options for
> [internal use / public redistribution of derived products].

Full candidate list with dates, sensors, and cloud cover:
`docs/imagery_candidates.txt`.

### Where to send it

- **Airbus** — OneAtlas / GeoStore. Pléiades 50 cm archive is quoted from
  ~€3.80/km² with no minimum order, which suits these small AOIs.
- **Maxar** — direct or through a reseller (LAND INFO, Apollo Mapping,
  European Space Imaging). The catalogue IDs above are Maxar's.
- **Planet** — SkySat 50 cm archive, worth a comparison quote.

Ask specifically about **minimum order area**. Pléiades Neo at 30 cm carries a
25 km² floor, so `resolute` (6.25 km²) would bill at 4x its actual size — that
one is much better value at 50 cm.

### Possibly free

The **Polar Geospatial Center** distributes Maxar imagery at no cost to US
federally-funded researchers at US institutions under an active NSF Office of
Polar Programs award. PGC is the same organisation that produces the ArcticDEM
this project already uses, and their high-arctic holdings are excellent. If any
part of this work sits under federal funding, email PGC User Services before
paying anyone.

## Using what you buy

Drop the delivered GeoTIFF anywhere under `out/`, then:

```bash
IMAGERY_FILE=/out/fort_ross/purchased.tif \
  docker compose run --rm -e FORCE_TERRAIN=1 -e SITE_NAME=fort_ross terrain
```

The ingest path reprojects from whatever UTM zone it arrives in to EPSG:3413,
clips to the site footprint, and handles bit depth automatically:

- 8-bit deliveries pass through untouched
- 11/16-bit get the same highlight-preserving tone curve as the Sentinel-2 path,
  so snow keeps its texture instead of clipping to white
- single-band (WorldView-1 panchromatic) is expanded to greyscale RGB

Raise `IMAGERY_SIZE` once you have real detail to carry — 4096 or 8192 is
justified at 50 cm, where it was pure waste at 10 m.

**WorldView-1 is panchromatic only.** Several archive hits are WV01: 50 cm but
greyscale. WV02 (46 cm) and WV03 (31 cm) both carry colour.
