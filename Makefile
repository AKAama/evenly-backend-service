export SHELL := /bin/bash
export SHELLOPTS := errexit:pipefail

# 镜像基本信息
REGISTRY    ?= crpi-3xux8vqn35fw00z9.cn-shanghai.personal.cr.aliyuncs.com
IMAGE_REPO  ?= project_hub
APP_NAME    ?= evenly-backend-service
TAG         ?= $(shell cat .version)

IMAGE       := $(REGISTRY)/$(IMAGE_REPO)/$(APP_NAME):$(TAG)

# Docker
DOCKERFILE ?= Dockerfile
PLATFORMS  ?= linux/amd64,linux/arm64

# 默认目标
.PHONY: all
all: image-build-push

# 初始化 buildx
.PHONY: docker-buildx-init
docker-buildx-init:
	@docker buildx inspect >/dev/null 2>&1 || docker buildx create --use
	@docker buildx inspect --bootstrap >/dev/null

# 构建并推送多架构镜像
.PHONY: image-build-push
image-build-push: docker-buildx-init
	@echo "==> Building & pushing $(IMAGE)"
	docker buildx build \
		--platform $(PLATFORMS) \
		-f $(DOCKERFILE) \
		-t $(IMAGE) \
		--push .

# 本地构建（仅当前架构）
.PHONY: image-build-local
image-build-local:
	docker build \
		-f $(DOCKERFILE) \
		-t $(IMAGE) \
		-t $(APP_NAME):latest .

# 查看镜像架构
.PHONY: image-inspect
image-inspect:
	docker buildx imagetools inspect $(IMAGE)

# 登录阿里云镜像仓库
.PHONY: docker-login
docker-login:
	@echo "==> Logging in to Aliyun Registry"
	@docker login --username=$(ALIYUN_USERNAME) $(REGISTRY)
	@echo "==> Login successful"

# 推送镜像到阿里云
.PHONY: image-push
image-push: docker-login
	@echo "==> Pushing $(IMAGE)"
	docker push $(IMAGE)

# 打标签（用于本地构建后推送）
.PHONY: image-tag
image-tag:
	@docker tag $(APP_NAME):latest $(IMAGE)

# 完整构建并推送流程
.PHONY: build-push
build-push: image-build-local image-tag image-push

# 查看当前镜像信息
.PHONY: info
info:
	@echo "IMAGE: $(IMAGE)"
	@echo "REGISTRY: $(REGISTRY)"
	@echo "IMAGE_REPO: $(IMAGE_REPO)"
	@echo "APP_NAME: $(APP_NAME)"
	@echo "TAG: $(TAG)"

.PHONY: db-upgrade
db-upgrade:
	uv run python -m alembic upgrade head

.PHONY: db-downgrade
db-downgrade:
	uv run python -m alembic downgrade -1

.PHONY: db-revision
db-revision:
	uv run python -m alembic revision --autogenerate -m "$(m)"

.PHONY: dev-db-up
dev-db-up: check-docker
	docker compose up -d postgres redis

.PHONY: dev-db-down
dev-db-down: check-docker
	docker compose down

.PHONY: dev-db-reset
dev-db-reset: check-docker
	docker compose down -v
	docker compose up -d postgres redis
	uv run python -m alembic upgrade head

.PHONY: dev-api
dev-api:
	uv run python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000

.PHONY: doctor
doctor:
	uv run python - <<'PY'
	from app.config import settings
	from app.database import engine
	print(f"DATABASE_URL: {settings.database_url}")
	with engine.connect() as conn:
	    print(f"database: {conn.exec_driver_sql('select current_database()').scalar()}")
	    print("connection: ok")
	PY

.PHONY: check-docker
check-docker:
	@docker info >/dev/null 2>&1 || (echo "Docker is not running. Start Docker Desktop and try again." && exit 1)
