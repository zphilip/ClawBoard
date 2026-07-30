# HA Lite Server - Makefile
BINARY   := halite
VERSION  := $(shell git describe --tags --always --dirty 2>/dev/null || echo "0.1.0")
BUILDTIME:= $(shell date -u '+%Y-%m-%dT%H:%M:%SZ')
LDFLAGS  := -s -w -X main.version=$(VERSION) -X main.buildTime=$(BUILDTIME)

# ── Build targets ──────────────────────────────────────────────────────────────

.PHONY: all build clean run test

all: build

build:
	go build -ldflags "$(LDFLAGS)" -o $(BINARY) .

# Cross-compile for Raspberry Pi Zero 2 W (ARMv8 64-bit).
# Pi Zero 2 W runs a 64-bit kernel but can also run 32-bit.
# Use GOARCH=arm64 for 64-bit, GOARCH=arm GOARM=7 for 32-bit.
pi-zero:
	GOOS=linux GOARCH=arm64 go build -ldflags "$(LDFLAGS)" -o $(BINARY)-linux-arm64 .
	GOOS=linux GOARCH=arm GOARM=7 go build -ldflags "$(LDFLAGS)" -o $(BINARY)-linux-armv7 .

# Cross-compile for all Raspberry Pi models.
pi-all: pi-zero
	GOOS=linux GOARCH=arm GOARM=6 go build -ldflags "$(LDFLAGS)" -o $(BINARY)-linux-armv6 .

# ── x86/amd64 builds ─────────────────────────────────────────────────────────

# Linux x86_64 (most servers, desktops, Intel NUC, etc.)
linux-amd64:
	GOOS=linux GOARCH=amd64 go build -ldflags "$(LDFLAGS)" -o $(BINARY)-linux-amd64 .

# Linux 32-bit x86 (very old hardware, embedded x86)
linux-x86:
	GOOS=linux GOARCH=386 go build -ldflags "$(LDFLAGS)" -o $(BINARY)-linux-386 .

# macOS Intel (x86_64)
darwin-amd64:
	GOOS=darwin GOARCH=amd64 go build -ldflags "$(LDFLAGS)" -o $(BINARY)-darwin-amd64 .

# macOS Apple Silicon (M1/M2/M3/M4)
darwin-arm64:
	GOOS=darwin GOARCH=arm64 go build -ldflags "$(LDFLAGS)" -o $(BINARY)-darwin-arm64 .

# Windows x86_64
windows-amd64:
	GOOS=windows GOARCH=amd64 go build -ldflags "$(LDFLAGS)" -o $(BINARY)-windows-amd64.exe .

# All desktop/server platforms
desktop: linux-amd64 darwin-amd64 darwin-arm64 windows-amd64

# Every target
release: desktop pi-all

# Build for the host platform.
run: build
	./$(BINARY)

# Run with config file (set HALITE_CONFIG to override).
dev:
	HALITE_CONFIG=halite.yaml go run .

# ── Utility ────────────────────────────────────────────────────────────────────

clean:
	rm -f $(BINARY) $(BINARY)-linux-* $(BINARY)-darwin-* $(BINARY)-windows-*

test:
	go test ./...

vet:
	go vet ./...

fmt:
	go fmt ./...

# ── Deploy to Raspberry Pi (set PI_HOST env var) ────────────────────────────────

PI_HOST ?= raspberrypi.local
PI_USER ?= pi
PI_DIR  ?= /home/pi/ha-lite

deploy: pi-zero
	ssh $(PI_USER)@$(PI_HOST) "mkdir -p $(PI_DIR)"
	scp $(BINARY)-linux-arm64 $(PI_USER)@$(PI_HOST):$(PI_DIR)/$(BINARY)
	scp halite.yaml $(PI_USER)@$(PI_HOST):$(PI_DIR)/
	ssh $(PI_USER)@$(PI_HOST) "chmod +x $(PI_DIR)/$(BINARY)"

deploy-service: deploy
	scp halite.service $(PI_USER)@$(PI_HOST):/tmp/
	ssh $(PI_USER)@$(PI_HOST) "sudo mv /tmp/halite.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable halite && sudo systemctl restart halite"
	@echo "✔ Deployed and started. Check: ssh $(PI_USER)@$(PI_HOST) 'systemctl status halite'"