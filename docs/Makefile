.PHONY: dev docs sdk-reference whitepaper whitepaper-website design_whitepaper storage_whitepaper storage_whitepaper-website cbqs_whitepaper cbqs_whitepaper-website secrets_whitepaper secrets_whitepaper-website clean install_deps install_texlibs

CHROME ?= /Applications/Google Chrome.app/Contents/MacOS/Google Chrome

# Run mintlify dev server
dev:
	npx mintlify dev

# Generate static docs (mintlify build)
docs:
	npx mintlify build

# Regenerate the SDK reference from cowboy_sdk source (expects a sibling node/
# checkout, per the repo layout). Run after the SDK's public API changes.
sdk-reference:
	python3 scripts/gen_sdk_reference.py

# Generate whitepaper PDF (requires: pandoc, lualatex, eisvogel template)
whitepaper:
	cd whitepaper && pandoc cowboy-technical-whitepaper.md \
		-o cowboy-whitepaper.pdf \
		--template eisvogel \
		--pdf-engine=lualatex \
		-V colorlinks=true \
		-V linkcolor=blue \
		-V mainfont="Latin Modern Roman" \
		-V sansfont="Latin Modern Roman" \
		-V monofont="Latin Modern Mono" \
		-V mathfont="Latin Modern Math" \
		-H header.tex \
		--number-sections

# Build whitepaper and copy to website public directory
whitepaper-website: whitepaper
	cp whitepaper/cowboy-whitepaper.pdf ../../website/public/whitepaper.pdf

# Generate the design companion in Cowboy's Gallop visual system.
design_whitepaper:
	cd whitepaper && pandoc cowboy-design-decisions.md \
		-o .cowboy-design-decisions.html \
		--to=html5 \
		--standalone \
		--embed-resources \
		--resource-path=.:.. \
		--css gallop-whitepaper.css \
		--syntax-highlighting=pygments
	@profile_dir=$$(mktemp -d); \
		pdf_path="$(CURDIR)/whitepaper/cowboy-design-decisions.pdf"; \
		html_path="$(CURDIR)/whitepaper/.cowboy-design-decisions.html"; \
		chrome_pid=""; \
		trap 'test -z "$$chrome_pid" || kill "$$chrome_pid" 2>/dev/null || true; test -z "$$chrome_pid" || wait "$$chrome_pid" 2>/dev/null || true; rm -rf "$$profile_dir" "$$html_path"' EXIT; \
		rm -f "$$pdf_path"; \
		"$(CHROME)" \
			--headless=new \
			--disable-gpu \
			--allow-file-access-from-files \
			--no-pdf-header-footer \
			--user-data-dir="$$profile_dir" \
			--print-to-pdf="$$pdf_path" \
			"file://$$html_path" >/dev/null 2>&1 & \
		chrome_pid=$$!; \
		for attempt in $$(seq 1 120); do \
			test -s "$$pdf_path" && break; \
			sleep 0.25; \
		done; \
		test -s "$$pdf_path"

# Generate storage whitepaper PDF (requires: pandoc, lualatex, eisvogel template)
storage_whitepaper:
	cd whitepaper && pandoc cowboy-storage-whitepaper.md \
		-o cowboy-storage-whitepaper.pdf \
		--template eisvogel \
		--pdf-engine=lualatex \
		-V colorlinks=true \
		-V linkcolor=blue \
		-V mainfont="Latin Modern Roman" \
		-V sansfont="Latin Modern Roman" \
		-V monofont="Latin Modern Mono" \
		-V mathfont="Latin Modern Math" \
		-H header.tex \
		--number-sections

# Build storage whitepaper and copy to website public directory
storage_whitepaper-website: storage_whitepaper
	cp whitepaper/cowboy-storage-whitepaper.pdf ../../website/public/storage-whitepaper.pdf

# Generate CBQS whitepaper PDF (requires: pandoc, lualatex, eisvogel template)
cbqs_whitepaper:
	cd whitepaper && pandoc cowboy-cbqs-whitepaper.md \
		-o cowboy-cbqs-whitepaper.pdf \
		--template eisvogel \
		--pdf-engine=lualatex \
		-V colorlinks=true \
		-V linkcolor=blue \
		-V mainfont="Latin Modern Roman" \
		-V sansfont="Latin Modern Roman" \
		-V monofont="Latin Modern Mono" \
		-V mathfont="Latin Modern Math" \
		-H header.tex

# Build CBQS whitepaper and copy to website public directory
cbqs_whitepaper-website: cbqs_whitepaper
	cp whitepaper/cowboy-cbqs-whitepaper.pdf ../../website/public/cbqs-whitepaper.pdf

# Generate secrets whitepaper PDF (requires: pandoc, lualatex, eisvogel template)
secrets_whitepaper:
	cd whitepaper && pandoc cowboy-secrets-whitepaper.md \
		-o cowboy-secrets-whitepaper.pdf \
		--template eisvogel \
		--pdf-engine=lualatex \
		-V colorlinks=true \
		-V linkcolor=blue \
		-V mainfont="Latin Modern Roman" \
		-V sansfont="Latin Modern Roman" \
		-V monofont="Latin Modern Mono" \
		-V mathfont="Latin Modern Math" \
		-H header.tex

# Build secrets whitepaper and copy to website public directory
secrets_whitepaper-website: secrets_whitepaper
	cp whitepaper/cowboy-secrets-whitepaper.pdf ../../website/public/secrets-whitepaper.pdf

# Install LaTeX dependencies for whitepaper generation (macOS)
install_deps:
	brew install pandoc
	brew install --cask basictex
	@echo "Restart your shell or run: eval \"\$$(/usr/libexec/path_helper)\""
	@echo "Then run: make install_texlibs"

# Install TeX libraries (run after basictex is installed)
install_texlibs:
	sudo /Library/TeX/texbin/tlmgr update --self
	sudo /Library/TeX/texbin/tlmgr install \
		adjustbox babel-german background bidi collectbox csquotes everypage \
		filehook footmisc footnotebackref framed fvextra letltxmacro ly1 \
		mdframed mweights needspace pagecolor sourcecodepro sourcesanspro \
		titling ucharcat ulem unicode-math upquote xecjk xurl zref \
		lm lm-math lualatex-math luatexbase newunicodechar
	mkdir -p ~/.pandoc/templates
	curl -sL https://github.com/Wandmalfarbe/pandoc-latex-template/releases/latest/download/Eisvogel.tar.gz | tar xz -C ~/.pandoc/templates
	mv ~/.pandoc/templates/Eisvogel-*/eisvogel.latex ~/.pandoc/templates/eisvogel.latex 2>/dev/null || true

# Clean generated files
clean:
	rm -f whitepaper/cowboy-whitepaper.pdf
	rm -f whitepaper/cowboy-design-decisions.pdf
	rm -f whitepaper/.cowboy-design-decisions.html
	rm -f whitepaper/cowboy-storage-whitepaper.pdf
	rm -f whitepaper/cowboy-cbqs-whitepaper.pdf
	rm -f whitepaper/cowboy-secrets-whitepaper.pdf
	rm -rf .mintlify
