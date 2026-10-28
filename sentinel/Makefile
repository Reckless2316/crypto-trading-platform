.PHONY: build test vet fmt check golden

build:
	go build -o sentinel ./cmd/sentinel

test:
	go test -race ./...

vet:
	go vet ./...

fmt:
	gofmt -w .

check:
	@test -z "$$(gofmt -l .)" || (gofmt -l . && exit 1)
	go vet ./...
	go test -race ./...

# Rebuild testdata/talib_golden.csv; TALIB_PREFIX must point at a TA-Lib install.
golden:
	tools/talibgold/gen.sh
