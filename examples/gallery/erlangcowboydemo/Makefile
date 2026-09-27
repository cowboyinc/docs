.PHONY: build run clean

build:
	rebar3 escriptize

run: build
	./_build/default/bin/erlangcowboydemo

clean:
	rebar3 clean
	rm -rf _build
