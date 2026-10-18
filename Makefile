# Developer shortcuts for the Market Data Intelligence & Analytics stack.
#
# Targets come in two shapes:
#   dev_*    merges docker-compose.override.yml  -> the development stack
#   prod_*   forces -f docker-compose.yml only   -> the production shape
#
# GNU make is not installed on this Windows box by default. Three ways to run it:
#   - install it:  winget install ezwinports.make   (GNU make 4.4; winget is
#                  present here. GnuWin32.Make is a lighter 3.81 alternative.)
#   - use scripts/dev.ps1, which exposes the same commands natively in PowerShell
#   - run make in a throwaway container, no install needed:
#       docker run --rm -v "%CD%:/app" -w /app alpine:3.20 \
#         sh -c "apk add --no-cache make && make dev_build"
# See docs/docker_deployment.md.
#
# Override the compose binary if you need the v1 one:  make COMPOSE=docker-compose dev_up
#
# Building and starting are separate on purpose. dev_build / prod_build build the
# image; dev_up / prod_up start the already-built image; dev_rebuild / prod_rebuild
# do both, in order.

COMPOSE      ?= docker compose
PROD_COMPOSE := $(COMPOSE) -f docker-compose.yml

.DEFAULT_GOAL := help
.PHONY: help \
        dev_build dev_up dev_rebuild dev_down dev_restart dev_logs dev_ps \
        dev_shell dev_manage dev_migrate dev_makemigrations dev_test dev_clean \
        prod_config prod_build prod_up prod_rebuild prod_down

# --------------------------------------------------------------------------- #
# Development
# --------------------------------------------------------------------------- #

dev_up: ## Start the stack detached, using the already-built image
	$(COMPOSE) up -d

dev_down: ## Stop and remove containers (volumes are kept)
	$(COMPOSE) down

dev_restart: ## Restart the web and worker processes
	$(COMPOSE) restart web worker

dev_logs: ## Follow the stack logs (Ctrl+C to stop)
	$(COMPOSE) logs -f --tail=100

dev_ps: ## Show container status and health
	$(COMPOSE) ps

dev_shell: ## Open a Django shell in the web container
	$(COMPOSE) exec web python manage.py shell

dev_manage: ## Run an arbitrary manage.py command: make dev_manage ARGS=migrate
	$(COMPOSE) exec web python manage.py $(ARGS)

dev_migrate: ## Apply migrations (the same one-shot service compose uses)
	$(COMPOSE) run --rm migrate

dev_makemigrations: ## Create new migration files from model changes
	$(COMPOSE) exec web python manage.py makemigrations

dev_test: ## Run the test suite inside the web container
	$(COMPOSE) exec web python -m pytest -q

dev_build: ## Build the image without starting anything
	$(COMPOSE) build

dev_rebuild: ## Build the image, then start the stack
	$(COMPOSE) build
	$(COMPOSE) up -d

dev_clean: ## Stop and DELETE volumes (DESTRUCTIVE: wipes the database)
	$(COMPOSE) down -v --remove-orphans

# --------------------------------------------------------------------------- #
# Production shape (no override file)
# --------------------------------------------------------------------------- #

prod_config: ## Render the production configuration and exit
	$(PROD_COMPOSE) config

prod_build: ## Build the production image
	$(PROD_COMPOSE) build

prod_up: ## Start the production shape detached, using the built image
	$(PROD_COMPOSE) up -d

prod_rebuild: ## Build the production image, then start the shape
	$(PROD_COMPOSE) build
	$(PROD_COMPOSE) up -d

prod_down: ## Stop the production shape (volumes are kept)
	$(PROD_COMPOSE) down

# --------------------------------------------------------------------------- #
# Help
# --------------------------------------------------------------------------- #

help: ## List the available shortcuts
	@echo --- Development: docker-compose.yml + docker-compose.override.yml ---
	@echo   make dev_build           Build the image without starting
	@echo   make dev_up              Start the stack detached, image must exist
	@echo   make dev_rebuild         Build the image, then start the stack
	@echo   make dev_down            Stop and remove containers, volumes kept
	@echo   make dev_restart         Restart the web and worker processes
	@echo   make dev_logs            Follow the stack logs
	@echo   make dev_ps              Show container status and health
	@echo   make dev_shell           Open a Django shell in the web container
	@echo   make dev_manage ARGS=... Run an arbitrary manage.py command
	@echo   make dev_migrate         Apply migrations
	@echo   make dev_makemigrations  Create new migration files
	@echo   make dev_test            Run the test suite in the web container
	@echo   make dev_clean           Stop and DELETE volumes, destructive
	@echo --- Production: docker-compose.yml only ---
	@echo   make prod_config         Render the production configuration
	@echo   make prod_build          Build the production image
	@echo   make prod_up             Start the production shape, image must exist
	@echo   make prod_rebuild        Build the production image, then start
	@echo   make prod_down           Stop the production shape
