# arctic-sim
#
#   make up        terrain + sim + SITL, then open http://localhost:8080
#   make terrain   (re)build just the terrain for the site in .env
#   make sim       terrain + Gazebo + gzweb, no vehicle
#   make logs      follow everything
#   make down      stop

SHELL := /bin/bash
COMPOSE := docker compose

.PHONY: up sim terrain rebuild-terrain add-site use-site sites build logs \
        shell-sim shell-sitl down clean

up:
	$(COMPOSE) up -d --build
	@echo
	@echo "  gzweb    http://localhost:$${GZWEB_PORT:-8080}"
	@echo "  MAVLink  tcp://localhost:5760   udp://localhost:14550"
	@echo
	@echo "  make logs   to follow startup"

# Terrain and viewer only — useful for checking a new site before waiting on
# the ArduPilot build.
sim:
	$(COMPOSE) up -d --build sim
	@echo "  gzweb  http://localhost:$${GZWEB_PORT:-8080}"

terrain:
	$(COMPOSE) run --rm terrain

# Force a fresh download even if the world already exists.
rebuild-terrain:
	$(COMPOSE) run --rm -e FORCE_TERRAIN=1 terrain

# Build an additional site without touching .env. Sites accumulate; each gets
# its own terrain model, so they never overwrite one another.
#   make add-site NAME=pond_inlet LAT=72.6989 LON=-77.9647 EXTENT=5000 GRID=513
add-site:
	@test -n "$(NAME)" || (echo "usage: make add-site NAME=x LAT=y LON=z [EXTENT=] [GRID=]"; exit 1)
	SITE_NAME=$(NAME) SITE_LAT=$(LAT) SITE_LON=$(LON) \
	SITE_EXTENT=$(or $(EXTENT),6500) SITE_GRID=$(or $(GRID),1025) \
	$(COMPOSE) run --rm terrain

# Point the running sim at an already-built site.
#   make use-site NAME=pond_inlet
use-site:
	@test -f sim/worlds/$(NAME).world || (echo "no such site: $(NAME)"; \
		echo "built sites:"; $(MAKE) -s sites; exit 1)
	$(COMPOSE) rm -sf sim sitl >/dev/null 2>&1 || true
	SITE_NAME=$(NAME) $(COMPOSE) up -d sim sitl
	@echo "now serving $(NAME) at http://localhost:$${GZWEB_PORT:-8080}"
	@echo "(edit SITE_NAME in .env to make it the default)"

# List built sites with their coordinates and size.
sites:
	@for w in sim/worlds/*.world; do \
		n=$$(basename $$w .world); \
		python3 -c "import json,sys; \
m=json.load(open('out/$$n/terrain.json')); \
print('  %-16s %9.5f, %10.5f  %5.0f m  %d^2  %.0f-%.0f m' % ('$$n', \
m['location']['lat'], m['location']['lon'], m['extent_m'], m['grid'], \
m['elevation_m']['min'], m['elevation_m']['max']))" 2>/dev/null || echo "  $$n"; \
	done

build:
	$(COMPOSE) build

logs:
	$(COMPOSE) logs -f

shell-sim:
	$(COMPOSE) exec sim bash

shell-sitl:
	$(COMPOSE) exec sitl bash

down:
	$(COMPOSE) down

# Drops generated terrain and the world; keeps images.
clean:
	$(COMPOSE) down -v
	rm -rf out/* sim/worlds/*.world sim/models/arctic_terrain
