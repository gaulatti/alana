# Makefile for alana

IMAGE_NAME       ?= alana
IMAGE_TAG        ?= amd64
PLATFORM         ?= linux/amd64
DOCKERFILE       ?= Dockerfile
CONTAINER_NAME   ?= alana
SHM_SIZE         ?= 1g

# Export this in your shell: export YOUTUBE_STREAM_KEY=...
YOUTUBE_STREAM_KEY ?=

# URL of the React channel page to render and stream
CHANNEL_BROWSER_URL ?= 
RESOLUTION ?=
WINDOW_SIZE ?=
FPS ?=
DRAW_MOUSE ?=
VIDEO_ENCODER ?=
VIDEO_BITRATE ?=
VIDEO_MAXRATE ?=
VIDEO_BUFSIZE ?=
X264_PRESET ?=
GOP_SIZE ?=
AUDIO_BITRATE ?=
VAAPI_DEVICE ?=
VAAPI_DRIVER ?=
DISABLE_CHROME_GPU ?=
CHROME_SOFTWARE_GL ?=
CHROME_DISABLE_DEV_SHM_USAGE ?=
CHROME_ENABLE_PERF_FLAGS ?=
CHROME_EXTRA_FLAGS ?=
FFMPEG_LOGLEVEL ?=
STREAM_MODE ?=
ICECAST_HOST ?=
ICECAST_PORT ?=
ICECAST_MOUNT ?=
ICECAST_SOURCE_PASSWORD ?=
ICECAST_CODEC ?=
ICECAST_BITRATE ?=
ICECAST_STREAM_NAME ?=

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
		--shm-size=$(SHM_SIZE) \
		$(DEVICE_FLAGS) \
		-e YOUTUBE_STREAM_KEY="$(YOUTUBE_STREAM_KEY)" \
		-e CHANNEL_BROWSER_URL="$(CHANNEL_BROWSER_URL)" \
		-e RESOLUTION="$(RESOLUTION)" \
		-e WINDOW_SIZE="$(WINDOW_SIZE)" \
		-e FPS="$(FPS)" \
		-e DRAW_MOUSE="$(DRAW_MOUSE)" \
		-e VIDEO_ENCODER="$(VIDEO_ENCODER)" \
		-e VIDEO_BITRATE="$(VIDEO_BITRATE)" \
		-e VIDEO_MAXRATE="$(VIDEO_MAXRATE)" \
		-e VIDEO_BUFSIZE="$(VIDEO_BUFSIZE)" \
		-e X264_PRESET="$(X264_PRESET)" \
		-e GOP_SIZE="$(GOP_SIZE)" \
		-e AUDIO_BITRATE="$(AUDIO_BITRATE)" \
		-e VAAPI_DEVICE="$(VAAPI_DEVICE)" \
		-e VAAPI_DRIVER="$(VAAPI_DRIVER)" \
		-e DISABLE_CHROME_GPU="$(DISABLE_CHROME_GPU)" \
		-e CHROME_SOFTWARE_GL="$(CHROME_SOFTWARE_GL)" \
		-e CHROME_DISABLE_DEV_SHM_USAGE="$(CHROME_DISABLE_DEV_SHM_USAGE)" \
		-e CHROME_ENABLE_PERF_FLAGS="$(CHROME_ENABLE_PERF_FLAGS)" \
		-e CHROME_EXTRA_FLAGS="$(CHROME_EXTRA_FLAGS)" \
		-e FFMPEG_LOGLEVEL="$(FFMPEG_LOGLEVEL)" \
		-e STREAM_MODE="$(STREAM_MODE)" \
		-e ICECAST_HOST="$(ICECAST_HOST)" \
		-e ICECAST_PORT="$(ICECAST_PORT)" \
		-e ICECAST_MOUNT="$(ICECAST_MOUNT)" \
		-e ICECAST_SOURCE_PASSWORD="$(ICECAST_SOURCE_PASSWORD)" \
		-e ICECAST_CODEC="$(ICECAST_CODEC)" \
		-e ICECAST_BITRATE="$(ICECAST_BITRATE)" \
		-e ICECAST_STREAM_NAME="$(ICECAST_STREAM_NAME)" \
		$(IMAGE_NAME):$(IMAGE_TAG)

shell:
	@echo ">> Starting interactive shell in $(CONTAINER_NAME) ..."
	docker run --rm -it \
		--platform $(PLATFORM) \
		--name=$(CONTAINER_NAME)-shell \
		--shm-size=$(SHM_SIZE) \
		$(DEVICE_FLAGS) \
		-e YOUTUBE_STREAM_KEY="$(YOUTUBE_STREAM_KEY)" \
		-e CHANNEL_BROWSER_URL="$(CHANNEL_BROWSER_URL)" \
		-e RESOLUTION="$(RESOLUTION)" \
		-e WINDOW_SIZE="$(WINDOW_SIZE)" \
		-e FPS="$(FPS)" \
		-e DRAW_MOUSE="$(DRAW_MOUSE)" \
		-e VIDEO_ENCODER="$(VIDEO_ENCODER)" \
		-e VIDEO_BITRATE="$(VIDEO_BITRATE)" \
		-e VIDEO_MAXRATE="$(VIDEO_MAXRATE)" \
		-e VIDEO_BUFSIZE="$(VIDEO_BUFSIZE)" \
		-e X264_PRESET="$(X264_PRESET)" \
		-e GOP_SIZE="$(GOP_SIZE)" \
		-e AUDIO_BITRATE="$(AUDIO_BITRATE)" \
		-e VAAPI_DEVICE="$(VAAPI_DEVICE)" \
		-e VAAPI_DRIVER="$(VAAPI_DRIVER)" \
		-e DISABLE_CHROME_GPU="$(DISABLE_CHROME_GPU)" \
		-e CHROME_SOFTWARE_GL="$(CHROME_SOFTWARE_GL)" \
		-e CHROME_DISABLE_DEV_SHM_USAGE="$(CHROME_DISABLE_DEV_SHM_USAGE)" \
		-e CHROME_ENABLE_PERF_FLAGS="$(CHROME_ENABLE_PERF_FLAGS)" \
		-e CHROME_EXTRA_FLAGS="$(CHROME_EXTRA_FLAGS)" \
		-e FFMPEG_LOGLEVEL="$(FFMPEG_LOGLEVEL)" \
		-e STREAM_MODE="$(STREAM_MODE)" \
		-e ICECAST_HOST="$(ICECAST_HOST)" \
		-e ICECAST_PORT="$(ICECAST_PORT)" \
		-e ICECAST_MOUNT="$(ICECAST_MOUNT)" \
		-e ICECAST_SOURCE_PASSWORD="$(ICECAST_SOURCE_PASSWORD)" \
		-e ICECAST_CODEC="$(ICECAST_CODEC)" \
		-e ICECAST_BITRATE="$(ICECAST_BITRATE)" \
		-e ICECAST_STREAM_NAME="$(ICECAST_STREAM_NAME)" \
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
