sphinx_docs:
	mkdir -p docs/reference
	sphinx-apidoc -o ./docs/reference ./application_sdk --tocfile index --module-first --separate --force
	cd docs && make html