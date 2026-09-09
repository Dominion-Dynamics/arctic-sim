# AWS deployment

What differs on an EC2 instance from a laptop checkout. The blueprint
(`infra/modules/blueprints/arctic-sim`) provisions the HOST — driver, Xorg,
Docker, tailnet, WireGuard. This directory covers the repo side.

## Install

```bash
rsync -az --delete --exclude .git --exclude __pycache__ --exclude node_modules \
      --exclude docker-compose.override.yml \
      ./ ubuntu@<sim>:/opt/arctic-sim/

ssh ubuntu@<sim> 'cd /opt/arctic-sim &&
  ln -sfn deploy/aws/docker-compose.override.yml docker-compose.override.yml &&
  docker compose up -d --build'
```

`--exclude docker-compose.override.yml` is not optional. Without it `--delete`
removes the symlink, Compose stops loading the overlay, and the next recreate
silently drops the GPU.

Sync `out/` rather than excluding it: the terrain pipeline skips any site whose
`terrain.json` still matches, so shipping built worlds means the instance never
re-downloads ArcticDEM or spends Mapbox tiles, and every box gets a
byte-identical albedo and heightmap.

## `.env` settings that differ from a laptop

```sh
CAMERAS=live
CONTROL_BIND=10.99.0.1     # WireGuard address: panel reachable to competitors
GZWEB_ASSET_MAX_AGE=300    # see below
```

`GZWEB_ASSET_MAX_AGE` defaults to 0, which revalidates every asset on reload —
0 bytes but ~55 round trips. Free on localhost, not over a tailnet or from a
cloud host. 300 makes a reload cost ~0 bytes and ~0 requests. The trade: after a
terrain rebuild a browser will not re-ask until it expires, so pair
`GZWEB_REDEPLOY=1` with a hard reload while iterating on terrain.

`CONTROL_BIND=10.99.0.1` binds the control API to the WireGuard address ONLY,
so organisers on the tailnet cannot reach `:8090`. Use `0.0.0.0` if you want
both; the security group admits neither from the internet.

## Verifying the GPU

The one symptom that separates a working deployment from an expensive CPU one:

```bash
docker compose logs sim | grep 'OpenGL renderer'   # want Tesla T4/PCIe/SSE2
docker inspect arctic-sim --format '{{.HostConfig.Runtime}}'   # want nvidia
nvidia-smi                                         # gzserver MUST appear
```

`llvmpipe`, or an `nvidia-smi` listing only `Xorg`, means the overlay is not
applied — whatever the startup log said when the container was first created.

Then confirm it survives the panel: `curl -X POST http://<bind>:8090/api/reset`
and check all three again. That is the regression this overlay exists to stop.
