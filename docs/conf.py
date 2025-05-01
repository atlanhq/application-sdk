import os
import sys

sys.path.insert(0, os.path.abspath(".."))

project = "application-sdk"
copyright = "2024, Phoenix, Atlan"
author = "Phoenix Team"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "myst_parser",
    "sphinx.ext.graphviz",
]
templates_path = ["_templates"]

html_theme = "pydata_sphinx_theme"
html_title = "application-sdk"

html_sidebars = {
    "reference/**": ["sidebar-nav-bs", "sidebar-ethical-ads"],
}

html_theme_options = {"github_url": "", "footer_start": ["copyright"]}
graphviz_output_format = "svg"
maximum_signature_line_length = 80

master_doc = "index"
source_suffix = [".rst", ".md"]
