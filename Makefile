.PHONY: verify test demo clean

verify: test
	@echo "all checks green"

test:
	npm test

demo:
	node --enable-source-maps server/demo.mjs

clean:
	rm -rf .data
