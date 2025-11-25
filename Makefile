# Makefile for alana

IMAGE_NAME       ?= alana
IMAGE_TAG        ?= amd64
PLATFORM         ?= linux/amd64
DOCKERFILE       ?= Dockerfile
CONTAINER_NAME   ?= alana

# You can export this in your shell: export YOUTUBE_STREAM_KEY=...
YOUTUBE_STREAM_KEY ?=

# Optional device mapping (used only on Linux host)
# On your Linux box you can run:
#   make run DEVICE_FLAGS="--device=/dev/dri/renderD129:/dev/dri/renderD129"
DEVICE_FLAGS ?=

.PHONY: build run shell stop rm logs restart clean

build:
	@echo ">> Building $(IMAGE_NAME):$(IMAGE_TAG) for $(PLATFORM) with buildx (and loading into local Docker)..."
	docker buildx build \
		--platform $(PLATFORM) \
		-t $(IMAGE_NAME):$(IMAGE_TAG) \
		-f $(DOCKERFILE) \
		--load \
		.

run:
	@echo ">> Running container $(CONTAINER_NAME) from $(IMAGE_NAME):$(IMAGE_TAG) ..."
	-docker stop $(CONTAINER_NAME) 2>/dev/null || true
	-docker rm $(CONTAINER_NAME) 2>/dev/null || true
	docker run -d \
		--platform $(PLATFORM) \
		--name=$(CONTAINER_NAME) \
		--restart=always \
		--shm-size=1g \
		$(DEVICE_FLAGS) \
		-v "$(PWD)/music":/media \
		-v "$(PWD)/video":/video \
		-e YOUTUBE_STREAM_KEY="$(YOUTUBE_STREAM_KEY)" \
		-p 4455:4455 \
		-p 5901:5901 \
		$(IMAGE_NAME):$(IMAGE_TAG)

shell:
	@echo ">> Starting interactive shell in $(CONTAINER_NAME) ..."
	docker run --rm -it \
		--platform $(PLATFORM) \
		--name=$(CONTAINER_NAME)-shell \
		--shm-size=1g \
		$(DEVICE_FLAGS) \
		-v "$(PWD)/music":/media \
		-v "$(PWD)/video":/video \
		-e YOUTUBE_STREAM_KEY="$(YOUTUBE_STREAM_KEY)" \
		-p 4455:4455 \
		-p 5901:5901 \
		$(IMAGE_NAME):$(IMAGE_TAG) \
		bash

stop:
	@echo ">> Stopping $(CONTAINER_NAME) ..."
	-docker stop $(CONTAINER_NAME) || true

rm:
	@echo ">> Removing container $(CONTAINER_NAME) ..."
	-docker rm $(CONTAINER_NAME) || true

logs:
	@echo ">> Tailing logs for $(CONTAINER_NAME) ..."
	docker logs -f $(CONTAINER_NAME)

restart: stop run

clean:
	@echo ">> Removing image $(IMAGE_NAME):$(IMAGE_TAG) ..."
	-docker rmi $(IMAGE_NAME):$(IMAGE_TAG) || true
