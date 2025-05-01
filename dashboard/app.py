import os

import dash
import dash_bootstrap_components as dbc
from dash import Dash, html

APP_DASHBOARD_PORT = int(os.getenv("ATLAN_APP_DASHBOARD_HTTP_PORT", 8050))
APP_DASHBOARD_HOST = os.getenv("ATLAN_APP_DASHBOARD_HTTP_HOST", "0.0.0.0")

dbc_css = "https://cdn.jsdelivr.net/gh/AnnMarieW/dash-bootstrap-templates/dbc.min.css"
app = Dash(
    __name__,
    use_pages=True,
    assets_folder="assets",
    external_stylesheets=[dbc.themes.COSMO, dbc_css],
)
app.title = "Atlan Application Dashboard"

app.layout = html.Div(
    [
        dbc.NavbarSimple(
            children=[
                dbc.NavItem(dbc.NavLink(page["name"], href=page["relative_path"]))
                for page in dash.page_registry.values()
            ],
            brand="Atlan Application Dashboard",
            brand_href="#",
            color="primary",
            dark=True,
            sticky="top",
        ),
        dash.page_container,
    ]
)

if __name__ == "__main__":
    app.run(host=APP_DASHBOARD_HOST, port=APP_DASHBOARD_PORT, debug=False)
