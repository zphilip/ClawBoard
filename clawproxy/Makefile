BINARY   := clawproxy
GOOS     ?= linux
GOARCH   ?= arm64

.PHONY: all build arm64 host clean

## default: build for linux/arm64  (target device)
all: arm64

arm64:
	GOOS=linux GOARCH=arm64 go build -o $(BINARY)-arm64 .
	GOOS=linux GOARCH=arm64 go build -o testclient-arm64 ./testclient/

## build for the current host  (dev / testing)
host:
	go build -o $(BINARY) .
	go build -o testclient ./testclient/

## build for both
build: arm64 host

clean:
	rm -f $(BINARY) $(BINARY)-arm64 testclient testclient-arm64
